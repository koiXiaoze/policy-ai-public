"""
Agent Service 层
封装现有业务能力（Scraper / Database / Analyzer / Reporter）为统一 Service。
Tool 层只调用 Service，不直接操作底层细节。
"""

from typing import Any, Dict, List, Optional
from pathlib import Path
from datetime import datetime
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy import or_
import re

from app.database import SessionLocal
from app.config import settings
from app.models.policy import Policy, Analysis
from app.analyzers.ai_analyzer import (
    ANALYSIS_STATUS_FAILED,
    analyze_policy,
    is_trusted_analysis,
)
from app.analysis_persistence import (
    prepare_trusted_analysis,
    sync_analysis_to_knowledge_base,
    upsert_analysis,
)
from app.scrapers.factory import run_all_scrapers
from app.reporters.daily_report import generate_daily_report, save_report
from app.data_layer import (
    source_registry, data_router, knowledge_base,
    SourceType, PolicyItem,
    query_parser, source_validator, date_filter, policy_classifier, result_ranker,
    TimeRange, PolicyQuery,
)
from app.data_layer.source_validator import passes_authority_domain_gate
from app.logger import logger


def _launch_content_backfill(created: int) -> None:
    """启动后台正文回填。

    将线程启动收口为独立入口，生产逻辑保持异步；测试可替换该入口，
    避免单元测试触发不受控的网络访问和共享 SQLite 写入。
    """
    if created <= 0:
        return
    try:
        import threading

        def _bg_backfill():
            try:
                from app.tools.backfill_content import backfill
                stats = backfill(limit=created, source=None)
                logger.info(
                    "后台正文回填完成: 成功 %s / 待补 %s",
                    stats.get("filled"), stats.get("missing"),
                )
            except Exception as exc:
                logger.warning("后台正文回填失败: %s", exc)

        threading.Thread(
            target=_bg_backfill, name="backfill-content", daemon=True
        ).start()
        logger.info("已启动后台正文回填任务（%s 条）", created)
    except Exception as exc:
        logger.warning("启动后台回填失败: %s", exc)


class PolicyService:
    """政策查询/分析 Service"""

    @staticmethod
    def _policy_item_to_dict(item: PolicyItem) -> Dict[str, Any]:
        """将 PolicyItem 转换为字典格式（兼容现有 Agent 接口）"""
        return {
            "id": item.raw_data.get("id") if item.raw_data else None,
            "title": item.title,
            "department": item.department,
            "government_level": item.government_level,
            "city": item.city,
            "publish_time": item.publish_time.isoformat() if item.publish_time else None,
            "url": item.url,
            "source_name": item.source_name,
            "importance_score": item.importance_score,
            "has_content": bool(item.content and len(item.content) > 100),
            "source_type": item.source_type.value,
            "content": item.content,
            "summary": item.summary,
        }

    @staticmethod
    def search_policies(region: Optional[str] = None, keyword: Optional[str] = None,
                        date_range: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
        """搜索政策 - Data Router 智能路由 + Filter Layer 过滤排序"""
        query_text = f"{region or ''} {keyword or ''} {date_range or ''} 政策搜索"
        policy_query = query_parser.parse(query_text)

        if date_range and not policy_query.time_range:
            time_query = query_parser.parse(date_range)
            if time_query.time_range:
                policy_query.time_range = time_query.time_range
                policy_query.custom_start = time_query.custom_start
                policy_query.custom_end = time_query.custom_end

        # 库内优先：本地已有足够新数据时，跳过实时抓取。
        # 每次消息都抓官网会让回复慢 30-60 秒（WAF 拦截 + Tavily 回退
        # + 详情抓取）。数据新鲜度由定时采集保证，问答只读缓存。
        cached = PolicyService._search_local_cached(
            region=region, keyword=keyword, date_range=date_range, limit=limit
        )
        if cached and PolicyService._is_cache_fresh(cached, max_age_hours=6):
            logger.info(
                "PolicyService.search_policies: 命中本地缓存 %s 条，跳过实时抓取",
                len(cached),
            )
            return cached

        decision = data_router.route(query=query_text, region=region, keyword=keyword)
        raw_results = data_router.execute_search(
            decision=decision,
            region=region,
            keyword=keyword,
            date_range=date_range,
            limit=limit * 2
        )
        logger.info(f"PolicyService.search_policies: routed via {decision.query_type.value}, raw={len(raw_results)}")

        # 实时抓取到的新政策必须即时入库。
        # 否则每条新政策只在当次回复里出现，下一次查询又回到旧库数据，
        # 用户会看到"最新政策"停留在几天前（实测湖北 10-12 政策
        # 抓到却未入库，导致查询一直返回 09-21 那条）。
        PolicyService._persist_new_items(raw_results)

        start, end = query_parser.get_date_bounds(policy_query)
        ranked_results = result_ranker.filter_and_rank(
            items=[PolicyService._policy_item_to_dict(r) for r in raw_results],
            query=query_text,
            time_range=policy_query.time_range,
            custom_start=policy_query.custom_start,
            custom_end=policy_query.custom_end,
            policy_only=policy_query.policy_only,
            min_score=20
        )
        logger.info(f"PolicyService.search_policies: after filter={len(ranked_results)}")

        if policy_query.authority:
            before_gate = len(ranked_results)
            ranked_results = [
                item for item in ranked_results
                if passes_authority_domain_gate(item.get("url", ""), policy_query.authority)
            ]
            removed = before_gate - len(ranked_results)
            if removed:
                logger.info(f"PolicyService.search_policies: authority gate removed {removed} (authority={policy_query.authority})")

        return ranked_results[:limit]

    @staticmethod
    def _passes_policy_path(source_name: Optional[str], url: str) -> bool:
        """按来源配置的栏目白名单校验 URL 路径。

        域名门禁只能保证"是同一个政府主站"，但主站下仍有新闻
        (/ywdt)、子站 (/show-*)、PDF 附件 (/uploadfile) 等非政策内容。
        这里再用栏目前缀做一次白名单过滤。
        """
        if not source_name or not url:
            return True
        try:
            from app.config import get_enabled_sources
            from urllib.parse import urlparse

            cfg = next(
                (s for s in get_enabled_sources() if s.get("name") == source_name),
                None,
            )
            prefixes = (cfg or {}).get("policy_path_prefixes") or []
            if not prefixes:
                return True
            path = urlparse(url).path or ""
            # PDF 等文件附件一律排除
            if path.lower().endswith(".pdf"):
                return False
            return any(path.startswith(prefix) for prefix in prefixes)
        except Exception as exc:
            logger.warning("栏目白名单校验失败，放行: %s", exc)
            return True

    @staticmethod
    def _region_to_authority(region: Optional[str]) -> Optional[str]:
        """把地区名映射为门禁表里的权威主体名。"""
        if not region:
            return None
        mapping = {
            "十堰": "十堰市人民政府", "十堰市": "十堰市人民政府",
            "武汉": "武汉市人民政府", "武汉市": "武汉市人民政府",
            "湖北": "湖北省人民政府", "湖北省": "湖北省人民政府",
            "国务院": "国务院", "人社部": "人力资源和社会保障部",
            "人力资源和社会保障部": "人力资源和社会保障部",
        }
        text = region.strip()
        for key, value in mapping.items():
            if key in text:
                return value
        return None

    #: 本地缓存视为"新鲜"的阈值（小时）。政府政策日内更新频率低，
    #: 6 小时足以覆盖，同时避免用户看到隔天旧数据。
    CACHE_FRESH_HOURS = 6

    @staticmethod
    def _search_local_cached(region: Optional[str], keyword: Optional[str],
                             date_range: Optional[str], limit: int) -> List[Dict[str, Any]]:
        """只查本地库，不做任何网络抓取。"""
        from app.data_layer.utils import parse_date_range
        from datetime import datetime, timedelta

        db = SessionLocal()
        try:
            query = db.query(Policy)
            if region:
                like = f"%{region.rstrip('省市')}%"
                query = query.filter(or_(
                    Policy.source_name.like(like),
                    Policy.department.like(like),
                    Policy.government_level.like(like),
                    Policy.city.like(like),
                ))
            if keyword:
                query = query.filter(Policy.title.contains(keyword))
            days = parse_date_range(date_range) if date_range else None
            if days:
                cutoff = datetime.now() - timedelta(days=days)
                query = query.filter(Policy.publish_time >= cutoff)

            rows = query.order_by(Policy.publish_time.desc()).limit(limit * 2).all()
            # 缓存结果同样要过来源门禁，否则历史脏数据（如 fzzx 子站）
            # 会绕过实时检索的门禁直接返回给用户。
            gate_authority = PolicyService._region_to_authority(region)
            filtered = []
            for p in rows:
                if gate_authority and not passes_authority_domain_gate(p.url, gate_authority):
                    continue
                if not PolicyService._passes_policy_path(p.source_name, p.url):
                    continue
                filtered.append(p)
                if len(filtered) >= limit:
                    break
            rows = filtered
            return [{
                "id": p.id,
                "title": p.title,
                "url": p.url,
                "source_name": p.source_name,
                "department": p.department,
                "government_level": p.government_level,
                "city": p.city,
                "publish_time": p.publish_time.isoformat() if p.publish_time else None,
                "content": p.content,
                "summary": p.summary,
                "has_content": bool(p.content and len(p.content.strip()) >= 100),
                "source_type": "local_db",
                # 记录入库时间：缓存新鲜度应按"数据何时被采集"判断，
                # 而不是按政策发布日期。湖北最新政策可能本身就是 7 天前发布，
                # 但今天刚采集过，仍应视为新鲜。
                "_cached_at": p.created_time.isoformat() if p.created_time else None,
            } for p in rows]
        except Exception as exc:
            logger.warning("本地缓存查询失败，回退实时抓取: %s", exc)
            return []
        finally:
            db.close()

    @classmethod
    def _is_cache_fresh(cls, rows: List[Dict[str, Any]], max_age_hours: int) -> bool:
        """判断缓存结果是否足够新（按最新一条的入库时间）。"""
        from datetime import datetime, timedelta

        if not rows:
            return False
        # 按入库时间判断：只要最近采集过，就视为新鲜。
        # 单条结果（如十堰仅有 1 条）同样可以命中缓存。

        stamps = [r.get("_cached_at") for r in rows if r.get("_cached_at")]
        if not stamps:
            return False
        try:
            latest = max(datetime.fromisoformat(str(s)[:19]) for s in stamps)
        except ValueError:
            return False
        return (datetime.now() - latest) <= timedelta(hours=max_age_hours)

    @staticmethod
    def _persist_new_items(items: List[Any]) -> int:
        """把实时抓取到的政策写入 Policy 表（按 URL 幂等）。

        只补建缺失记录，不覆盖已有正文/分析，避免用列表页的空正文
        冲掉数据库中已抓好的全文。
        """
        if not items:
            return 0

        db = SessionLocal()
        created = 0
        try:
            urls = [i.url for i in items if getattr(i, "url", None)]
            if not urls:
                return 0
            existing = {
                row[0] for row in
                db.query(Policy.url).filter(Policy.url.in_(urls)).all()
            }
            for item in items:
                url = getattr(item, "url", None)
                if not url or url in existing:
                    continue
                # 来源门禁：只收录该来源的官方主域，避免把政府子站
                # （如十堰市中小企业发展中心 fzzx.shiyan.gov.cn）
                # 当成市政府政策写入库。
                source_name = getattr(item, "source_name", None)
                if source_name and not passes_authority_domain_gate(url, source_name):
                    logger.debug(
                        "跳过非官方主域来源: source=%s url=%s",
                        source_name, url[:80],
                    )
                    continue
                if not PolicyService._passes_policy_path(source_name, url):
                    logger.debug(
                        "跳过非政策栏目: source=%s url=%s", source_name, url[:80],
                    )
                    continue
                db.add(Policy(
                    title=(item.title or "")[:500],
                    url=url,
                    department=getattr(item, "department", None),
                    government_level=getattr(item, "government_level", None),
                    city=getattr(item, "city", None),
                    source_name=getattr(item, "source_name", None),
                    publish_time=getattr(item, "publish_time", None),
                    content=getattr(item, "content", None),
                    summary=getattr(item, "summary", None),
                    keywords=getattr(item, "keywords", None),
                ))
                existing.add(url)
                created += 1
            if created:
                db.commit()
                logger.info("实时检索结果已入库: 新增 %s 条新政策", created)
        except Exception as exc:
            db.rollback()
            logger.warning("实时检索结果入库失败（不影响本次回复）: %s", exc)
        finally:
            db.close()

        # 新入库的记录只有列表页数据，没有正文。正文抓取每条 4-18 秒，
        # 同步执行会让单次回复多等几十秒。改为后台线程异步回填：
        # 本次回复不受影响，后续追问时正文通常已就绪。
        if created:
            _launch_content_backfill(created)
        return created

    @staticmethod
    def get_policy_detail(policy_id: Optional[int] = None, url: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """获取政策详情（支持ID或URL，跨数据源）"""
        if url:
            return PolicyService._get_detail_by_url(url)
        if policy_id:
            return PolicyService._get_detail_by_id(policy_id)
        return None

    @staticmethod
    def _get_detail_by_id(policy_id: int) -> Optional[Dict[str, Any]]:
        """通过ID查询本地数据库"""
        db = SessionLocal()
        try:
            policy = db.query(Policy).filter(Policy.id == policy_id).first()
            if not policy:
                return None
            analysis = db.query(Analysis).filter(Analysis.policy_id == policy_id).first()
            return {
                "id": policy.id,
                "title": policy.title,
                "department": policy.department,
                "government_level": policy.government_level,
                "city": policy.city,
                "publish_time": policy.publish_time.isoformat() if policy.publish_time else None,
                "url": policy.url,
                "source_name": policy.source_name,
                "content": policy.content,
                "summary": policy.summary,
                "keywords": policy.keywords,
                "importance_score": policy.importance_score,
                "analysis": {
                    "summary": analysis.summary,
                    "plain_explanation": analysis.plain_explanation,
                    "national_direction": analysis.national_direction,
                    "industry_impact": analysis.industry_impact,
                    "career_opportunity": analysis.career_opportunity,
                    "personal_advice": analysis.personal_advice,
                    "risk_analysis": analysis.risk_analysis,
                    "trend_score": analysis.trend_score,
                    "raw_response": analysis.raw_response
                } if analysis else None
            }
        finally:
            db.close()

    @staticmethod
    def _get_detail_by_url(url: str) -> Optional[Dict[str, Any]]:
        """通过URL获取政策详情（优先本地DB，再官方API，再Web搜索）"""
        db = SessionLocal()
        try:
            policy = db.query(Policy).filter(Policy.url == url).first()
            if policy:
                analysis = db.query(Analysis).filter(Analysis.policy_id == policy.id).first()
                return {
                    "id": policy.id,
                    "title": policy.title,
                    "department": policy.department,
                    "government_level": policy.government_level,
                    "city": policy.city,
                    "publish_time": policy.publish_time.isoformat() if policy.publish_time else None,
                    "url": policy.url,
                    "source_name": policy.source_name,
                    "content": policy.content,
                    "summary": policy.summary,
                    "keywords": policy.keywords,
                    "importance_score": policy.importance_score,
                    "analysis": {
                        "summary": analysis.summary,
                        "plain_explanation": analysis.plain_explanation,
                        "national_direction": analysis.national_direction,
                        "industry_impact": analysis.industry_impact,
                        "career_opportunity": analysis.career_opportunity,
                        "personal_advice": analysis.personal_advice,
                        "risk_analysis": analysis.risk_analysis,
                        "trend_score": analysis.trend_score,
                        "raw_response": analysis.raw_response
                    } if analysis else None,
                    "source_type": "local_db"
                }
        finally:
            db.close()

        official_source = source_registry.get_by_type(SourceType.OFFICIAL_API)
        if official_source:
            item = official_source.fetch_detail(url)
            if item:
                return PolicyService._policy_item_to_dict(item)

        web_source = source_registry.get_by_type(SourceType.WEB_SEARCH)
        if web_source:
            item = web_source.fetch_detail(url)
            if item:
                return PolicyService._policy_item_to_dict(item)

        return None

    @staticmethod
    def search_historical_policies(keyword: str, region: Optional[str] = None,
                                   time_range: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        """搜索历史政策（用于趋势分析）"""
        query_text = f"{region or ''} {keyword} 历史趋势"
        policy_query = query_parser.parse(query_text)
        policy_query.time_range = TimeRange.THIS_YEAR

        kb_results = knowledge_base.search(query=query_text, limit=limit * 2)

        items = []
        for entry in kb_results:
            items.append({
                "id": entry.id,
                "title": entry.title,
                "department": entry.metadata.get("raw_data", {}).get("department") if entry.metadata else None,
                "government_level": entry.government_level,
                "city": entry.metadata.get("raw_data", {}).get("city") if entry.metadata else None,
                "publish_time": entry.publish_time.isoformat() if entry.publish_time else None,
                "url": entry.url,
                "source_name": entry.source_name,
                "importance_score": entry.metadata.get("importance_score", 0) if entry.metadata else 0,
                "has_content": bool(entry.content),
                "source_type": entry.metadata.get("source_type", "local_db") if entry.metadata else "local_db",
                "category": entry.category,
                "keywords": entry.keywords,
            })

        if len(items) < limit:
            db_results = PolicyService.search_policies(keyword=keyword, region=region, date_range=time_range, limit=limit - len(items))
            items.extend(db_results)

        ranked = result_ranker.filter_and_rank(
            items=items,
            query=query_text,
            time_range=TimeRange.THIS_YEAR,
            policy_only=False,
            min_score=10
        )
        logger.info(f"PolicyService.search_historical_policies: knowledge_base={len(kb_results)}, final={len(ranked)}")
        return ranked[:limit]

    @staticmethod
    def analyze_policies(policies: List[Dict[str, Any]], top_n: Optional[int] = None,
                         concurrency: Optional[int] = None) -> Dict[str, Any]:
        """批量分析排序后的前 TOP N 条政策。

        这是 Agent 定时任务的单步批量入口：固定占 1 个工具步，避免
        “一条政策一个 Agent 步”让 TOP N 随步数上限被截断。单条
        analyze_policy 仍保留，供自然语言追问直接分析一段文本。

        并发边界：
        - 正文补抓只在主线程完成，Worker 不创建、不共享 SQLAlchemy Session；
        - 每条政策先深拷贝，Worker 只消费自己的不可共享输入；
        - 任一正文缺失、AI 异常或结果类型异常只影响当前条目。
        """
        from app.config import MAX_REPORT_TOP_N, get_report_top_n

        if not isinstance(policies, list):
            return {
                "success": False,
                "error": "policies 必须是政策列表",
                "requested": 0,
                "processed": 0,
                "succeeded": 0,
                "failed": 0,
                "results": [],
                "failures": [],
            }

        requested = get_report_top_n() if top_n is None else int(top_n)
        requested = max(1, min(requested, MAX_REPORT_TOP_N))
        selected = [deepcopy(item) for item in policies[:requested] if isinstance(item, dict)]
        if concurrency is None:
            concurrency = settings.analyze_concurrency
        try:
            worker_count = max(1, min(5, int(concurrency)))
        except (TypeError, ValueError):
            worker_count = 3

        failures: List[Dict[str, Any]] = []
        prepared: List[Dict[str, Any]] = []

        # 补正文属于数据库/网络 I/O，统一放主线程。Worker 只拿纯文本，
        # 因此不会出现多个线程共享 Session 或串写数据库的问题。
        for order, source in enumerate(selected):
            policy_id = source.get("policy_id") or source.get("id")
            url = source.get("url")
            title = source.get("title") or ""
            content = source.get("content") or ""

            if len(str(content).strip()) < 50:
                detail = None
                try:
                    # URL 是跨数据源的稳定业务键；仅在没有 URL 时才用 id，
                    # 避免把爬虫原始 id 误当成 Policy 表自增主键。
                    if url:
                        detail = PolicyService.get_policy_detail(url=url)
                    elif policy_id:
                        detail = PolicyService.get_policy_detail(policy_id=policy_id)
                except Exception as exc:
                    logger.warning(
                        "批量分析补正文异常: policy_id=%s url=%s %s",
                        policy_id, url, exc,
                    )
                    detail = None

                if isinstance(detail, dict):
                    content = detail.get("content") or content
                    title = source.get("title") or detail.get("title") or title
                    url = url or detail.get("url")

            if len(str(content).strip()) < 50:
                failures.append({
                    "policy_id": policy_id,
                    "url": url,
                    "title": title,
                    "error": "政策正文过短或无法补抓，无法分析",
                })
                continue

            prepared.append({
                "order": order,
                "policy_id": policy_id,
                "url": url,
                "title": title,
                "content": str(content),
            })

        def _run_one(item: Dict[str, Any]) -> tuple[Dict[str, Any], Any]:
            # 只调用 AI 分析；Worker 内不访问数据库。
            return item, analyze_policy(item["content"], item["title"])

        completed: List[tuple[int, Dict[str, Any]]] = []
        if prepared:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = {executor.submit(_run_one, item): item for item in prepared}
                for future in as_completed(futures):
                    item = futures[future]
                    try:
                        _, raw = future.result()
                    except Exception as exc:
                        failures.append({
                            "policy_id": item.get("policy_id"),
                            "url": item.get("url"),
                            "title": item.get("title"),
                            "error": f"AI 分析异常: {exc}",
                        })
                        continue

                    if not isinstance(raw, dict):
                        failures.append({
                            "policy_id": item.get("policy_id"),
                            "url": item.get("url"),
                            "title": item.get("title"),
                            "error": f"AI 分析返回了非字典结果: {type(raw).__name__}",
                        })
                        continue

                    result = deepcopy(raw)
                    # 绑定关系以 Service 选中的原始政策为权威，绝不使用
                    # AgentState.current_analyze_target，也不信任 LLM 回写的标题。
                    if item.get("policy_id") is not None:
                        result["policy_id"] = item["policy_id"]
                    if item.get("url"):
                        result["url"] = item["url"]
                    if item.get("title"):
                        result["title"] = item["title"]

                    if result.get("_analysis_status") == ANALYSIS_STATUS_FAILED:
                        failures.append({
                            "policy_id": item.get("policy_id"),
                            "url": item.get("url"),
                            "title": item.get("title"),
                            "error": result.get("_analysis_error") or "分析失败",
                            "_analysis_source": result.get("_analysis_source"),
                        })
                    else:
                        completed.append((item["order"], result))

        # 并发完成顺序不稳定；返回顺序仍与输入的排序结果一致。
        completed.sort(key=lambda pair: pair[0])
        results = [result for _, result in completed]
        succeeded = sum(1 for result in results if is_trusted_analysis(result))
        return {
            "success": True,
            "requested": requested,
            "processed": len(results) + len(failures),
            "succeeded": succeeded,
            "failed": len(failures),
            "results": results,
            "failures": failures,
        }

    @staticmethod
    def analyze_policy(policy_text: str, title: Optional[str] = None) -> Dict[str, Any]:
        """AI 政策分析"""
        if not policy_text or len(policy_text.strip()) < 50:
            return {"error": "政策正文过短，无法分析"}
        result = analyze_policy(policy_text, title or "")
        return result

    @staticmethod
    def save_analysis(policy_id: Optional[int], result: Dict[str, Any],
                      url: Optional[str] = None) -> bool:
        """把 AI 分析结果 upsert 到 Analysis 表。

        Agent 路径此前只把分析结果放进 AgentState，从不落 Analysis 表，
        而 CLI/workflow 路径会落库。两条路径行为不一致的直接后果是：
        1) get_policy_detail 读 Analysis 恒为 None，报告侧永远拿不到历史分析；
        2) 同一份分析无法被检索/知识库复用。

        必须先按 URL 解析出真实的 Policy 行再落库：
        Agent 侧 search_policies 返回的 "id" 来自 _policy_item_to_dict 里的
        item.raw_data["id"]，那是爬虫给的原始 id，不是 Policy 表的自增主键。
        直接拿它当 Analysis.policy_id 会把分析挂到不相干的政策上（SQLite
        默认不校验外键，不会报错，只是数据静默错位）。workflow 侧去重也是
        按 Policy.url 匹配的，这里沿用同一个业务主键。

        可信校验、中文字段映射、JSON 序列化和知识库同步统一由
        app.analysis_persistence 负责，避免 Agent 与 workflow 两条链路漂移。

        失败只记日志不抛异常：落库是分析之后的附加动作，数据库抖动不应该
        让已经拿到的分析结果和报告一起丢掉。
        """
        if not isinstance(result, dict) or result.get("error"):
            logger.info("分析结果异常或带 error，跳过落库")
            return False
        clean_result = prepare_trusted_analysis(result)
        if clean_result is None:
            logger.info(
                "非可信分析结果（source=%s, status=%s），跳过落库",
                result.get("_analysis_source"),
                result.get("_analysis_status"),
            )
            return False
        if not policy_id and not url:
            # 自然语言问答可能直接分析用户粘贴的文本，没有对应 Policy 行
            logger.info("分析结果无 policy_id 与 url，跳过落库")
            return False

        db = None
        try:
            db = SessionLocal()
            policy = None
            if url:
                policy = db.query(Policy).filter(Policy.url == url).first()
            if policy is None and policy_id:
                policy = db.query(Policy).filter(Policy.id == policy_id).first()
            if policy is None:
                # 库里没有这条政策：写了 Analysis 就是一条指向不存在 Policy 的
                # 孤儿行（SQLite 不拦外键），宁可跳过。
                logger.info(
                    "库中找不到对应政策（policy_id=%s, url=%s），跳过落库",
                    policy_id, url,
                )
                return False

            # workflow 侧按 URL 去重；这里使用解析出的真实主键，而不是
            # 调用方传入的爬虫原始 id。upsert 保证重复分析不撞唯一约束。
            is_new = db.query(Analysis).filter(Analysis.policy_id == policy.id).first() is None
            analysis = upsert_analysis(db, policy, clean_result)
            if analysis is None:
                logger.info("分析结果未通过持久化校验，跳过落库")
                return False
            db.commit()
            logger.info(
                "分析结果落库: policy_id=%s, url=%s, 评分=%s, %s",
                policy.id,
                url,
                clean_result.get("趋势评分", 0),
                "新增" if is_new else "更新",
            )

            # 在 commit 之后同步知识库；共享模块会自行捕获向量库异常，
            # 避免知识库故障回滚已经提交的分析结果。
            sync_analysis_to_knowledge_base(policy, clean_result)
            return True
        except Exception as e:
            if db is not None:
                db.rollback()
            logger.warning(
                "分析结果落库失败（不影响报告生成）: policy_id=%s, %s",
                policy_id,
                e,
            )
            return False
        finally:
            if db is not None:
                db.close()

    @staticmethod
    def collect_new_policies() -> Dict[str, Any]:
        """采集新政策并写入本地库（按 URL 幂等去重）。

        采集结果必须落库：Agent 后续的检索、分析、报告都只读 Policy 表，
        不落库等于每次都靠实时爬取，既慢又不稳定。
        """
        try:
            items = run_all_scrapers()
            saved = 0
            content_filled = 0
            if items:
                from app.workflow import _dedupe_and_save_policies
                db = SessionLocal()
                try:
                    new_policies = _dedupe_and_save_policies(db, items)
                    saved = len(new_policies)
                    if new_policies:
                        content_filled = PolicyService._fill_content_for(new_policies)
                        db.commit()
                finally:
                    db.close()
            logger.info(
                "采集落库完成: 抓到 %s 条, 新增 %s 条, 补正文 %s 条",
                len(items), saved, content_filled,
            )
            return {
                "success": True,
                "collected": len(items),
                "saved": saved,
                "content_filled": content_filled,
                "items": [
                    {
                        "title": item.title,
                        "url": item.url,
                        "source_name": item.source_name,
                        "government_level": item.government_level
                    }
                    for item in items[:10]
                ]
            }
        except Exception as e:
            logger.error(f"采集失败: {e}")
            return {"success": False, "error": str(e)}

    @staticmethod
    def _fill_content_for(policies: List[Policy]) -> int:
        """为新入库政策抓取正文。

        按发布时间倒序限量，避免单次任务耗时超过 AgentTask 超时阈值
        （正文抓取约 4-18 秒/条）。已入库且已有正文的不重复抓取。
        """
        from app.config import get_enabled_sources
        from app.scrapers.factory import create_scraper

        sources = get_enabled_sources()
        limit = max(0, settings.collect_content_limit)
        ordered = sorted(
            policies,
            key=lambda p: p.publish_time or datetime.min,
            reverse=True,
        )
        filled = 0
        for policy in ordered[:limit]:
            if policy.content and len(policy.content.strip()) >= 100:
                continue
            matched = [s for s in sources if s.get("name") == policy.source_name]
            if not matched:
                logger.debug("无匹配采集器，跳过正文: %s", policy.title[:30])
                continue
            try:
                content = create_scraper(matched[0]).extract_content(policy.url)
            except Exception as exc:
                logger.warning("正文抓取异常: %s - %s", policy.title[:30], exc)
                continue
            if content and len(content.strip()) >= 50:
                policy.content = content[:10000]
                filled += 1
        return filled


class ReportService:
    """报告生成 Service"""

    @staticmethod
    def _normalize_title(value: Optional[str]) -> str:
        """标题归一化：去掉空白与常见标点差异后再比较。

        政府网站标题里全角/半角括号、书名号、空格经常不一致，
        等值比较会把同一条政策判成两条。
        """
        if not value:
            return ""
        text = re.sub(r"[\s\u3000]+", "", str(value))
        return re.sub(r"[《》〈〉「」『』（）()\[\]【】\"'`、,。.:：;；\-—_~·]", "", text)

    @classmethod
    def _match_analysis(cls, policy: Dict[str, Any],
                        analyses: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """把一条政策匹配到它的分析结果。

        匹配键按可靠性排序：policy_id > url > 归一化标题。
        原实现只比 title/url 的等值，而 LLM 返回的 title 常被改写、
        url 又根本不在分析结果里，导致匹配全部失败、报告正文为空。
        """
        policy_id = policy.get("policy_id") or policy.get("id")
        url = policy.get("url")
        title_key = cls._normalize_title(policy.get("title"))

        fallback = None
        for analysis in analyses:
            if not isinstance(analysis, dict):
                continue
            analysis_id = analysis.get("policy_id")
            if policy_id and analysis_id and str(analysis_id) == str(policy_id):
                return analysis
            if url and analysis.get("url") and analysis["url"] == url:
                return analysis
            if title_key and cls._normalize_title(analysis.get("title")) == title_key:
                fallback = fallback or analysis
        return fallback

    @staticmethod
    def generate_report(policies: List[Dict[str, Any]], analyses: List[Dict[str, Any]],
                        mode: str = "daily", source_name: Optional[str] = None,
                        output_dir: str = "reports",
                        background_policies: Optional[List[Dict[str, Any]]] = None,
                        top_n: Optional[int] = None) -> Path:
        """生成政策报告

        output_dir 供测试隔离写入位置；线上行为不变，仍写 reports/。
        background_policies 是此前已推送过的政策，仅作为背景参考，不进入主区。
        """
        policies_with_analysis = []
        for policy in policies:
            analysis = ReportService._match_analysis(policy, analyses or [])
            policies_with_analysis.append({"policy": policy, "analysis": analysis})

        report_text = generate_daily_report(
            policies_with_analysis,
            source_name or "多源",
            background_policies=background_policies or [],
            mode=mode,
            top_n=top_n,
        )
        return save_report(report_text, output_dir=output_dir, mode=mode, source_name=source_name)
