
"""
主工作流
"""

from __future__ import annotations

import json as _json
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from app.logger import logger
from app.database import SessionLocal, init_db
from app.models.policy import Policy, Analysis
from app.scrapers.factory import create_scraper
from app.analyzers.ai_analyzer import analyze_policy
from app.analysis_persistence import (
    prepare_trusted_analysis,
    sync_analysis_to_knowledge_base,
    upsert_analysis,
)
from app.reporters.daily_report import generate_daily_report, save_report
from app.notifiers import get_enabled_notifiers
from app.notifiers.service import NotificationService
from app.config import get_enabled_sources, get_report_output_dir
from app.scrapers.content_cleaner import (
    clean_title,
    extract_date_from_url,
    normalize_text,
    normalize_url_for_dedupe,
)


import re


def _extract_date_from_html(soup, href: str):
    """从 HTML 中提取发布日期"""
    # 尝试从链接旁边的 span 标签获取日期
    parent = soup.find("a", href=href)
    if parent:
        # 查找同级的 span
        span = parent.find_next_sibling("span")
        if not span:
            # 尝试父级 li 下的 span
            li = parent.find_parent("li")
            if li:
                span = li.find("span")
        if span:
            date_str = span.get_text(strip=True)
            m = re.search(r"(\d{4}-\d{2}-\d{2})", date_str)
            if m:
                try:
                    return datetime.strptime(m.group(1), "%Y-%m-%d")
                except Exception:
                    pass
    # 尝试从 URL 中提取日期
    m = re.search(r"/(\d{4})(\d{2})/t\d{4}(\d{2})(\d{2})", href)
    if not m:
        m = re.search(r"/(\d{4})(\d{2})/t\d{4}_", href)
    if not m:
        m = re.search(r"(\d{4}-\d{2}-\d{2})", href)
    if m:
        try:
            if len(m.groups()) == 4:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(4)))
            elif len(m.groups()) == 2:
                return datetime(int(m.group(1)), int(m.group(2)), 1)
            else:
                return datetime.strptime(m.group(1), "%Y-%m-%d")
        except Exception:
            pass
    return None

def run_daily_pipeline() -> dict:
    result = {
        "success": False,
        "scraped": 0,
        "new": 0,
        "analyzed": 0,
        "report_path": None,
        "notify_results": {},
        "mode": "online",
        "errors": [],
        "partial_failures": {
            "content_extract": [],
            "analysis": [],
            "knowledge_base": [],
            "notification": [],
        },
    }
    logger.info("=" * 50)
    logger.info("开始执行每日政策情报流程")
    logger.info("=" * 50)
    init_db()
    db = SessionLocal()
    try:
        from app.scrapers.factory import run_all_scrapers
        raw_items = run_all_scrapers()
        result["scraped"] = len(raw_items)
        if result["scraped"] == 0:
            local_paths = _collect_local_paths()
            if local_paths:
                raw_items = _import_local_files(local_paths, days=1)
                result["scraped"] = len(raw_items)
                result["mode"] = "local"
        new_policies = _dedupe_and_save_policies(db, raw_items)
        result["new"] = len(new_policies)
        need_analysis = list(new_policies)
        for policy in _select_policies_needing_analysis(db, raw_items):
            if policy not in need_analysis:
                need_analysis.append(policy)
        policies_with_analysis, analysis_failures = (
            _fill_content_and_analyze_with_failures(db, need_analysis)
        )
        result["partial_failures"]["content_extract"].extend(
            analysis_failures["content_extract"]
        )
        result["partial_failures"]["analysis"].extend(
            analysis_failures["analysis"]
        )
        result["analyzed"] = len(policies_with_analysis)
        
        # 先提交 SQLite，再同步知识库。否则数据库提交失败时，知识库
        # 已经写入了不存在的分析结果，重试也无法自动回滚外部存储。
        db.commit()
        if policies_with_analysis:
            result["partial_failures"]["knowledge_base"] = (
                _sync_to_knowledge_base(policies_with_analysis)
            )
        if policies_with_analysis:
            report_text = generate_daily_report(policies_with_analysis)
            report_path = save_report(
                report_text,
                output_dir=get_report_output_dir(),
            )
            result["report_path"] = str(report_path)
            notify_results, notification_failures = (
                _send_report_notifications(report_path)
            )
            result["notify_results"] = notify_results
            result["partial_failures"]["notification"].extend(
                notification_failures
            )
        else:
            logger.info("无新政策分析，跳过报告生成")
        result["success"] = not result["errors"] and not any(
            result["partial_failures"].values()
        )
    except Exception as e:
        logger.error("流程执行失败: %s", e, exc_info=True)
        db.rollback()
        result["errors"].append({"stage": "pipeline", "error": str(e)})
    finally:
        db.close()
    logger.info("=" * 50)
    logger.info("流程完成: 采集 %s 条, 新增 %s 条, 分析 %s 条, 模式=%s", result["scraped"], result["new"], result["analyzed"], result["mode"])
    if result["report_path"]:
        logger.info("报告已保存: %s", result["report_path"])
        for name, notification_result in result.get("notify_results", {}).items():
            status = (
                notification_result.get("status", "unknown")
                if isinstance(notification_result, dict)
                else notification_result
            )
            logger.info("推送 [%s]: %s", name, status)
    logger.info("=" * 50)
    return result




def _sync_to_knowledge_base(policies_with_analysis: list) -> list:
    """同步全部可信分析，返回失败明细而不是静默吞掉。"""
    failures = []
    count = 0
    for item in policies_with_analysis:
        policy = item["policy"]
        try:
            synced = sync_analysis_to_knowledge_base(
                policy, item.get("analysis")
            )
        except Exception as exc:
            synced = False
            logger.warning(
                "知识库同步异常: url=%s, error=%s",
                getattr(policy, "url", ""),
                exc,
            )
        if synced:
            count += 1
        else:
            # 已存在按 URL 跳过属于幂等成功，不应被当作故障。
            try:
                from app.data_layer import knowledge_base as kb_module
                already_exists = any(
                    entry.url == getattr(policy, "url", None)
                    for entry in kb_module.knowledge_base.entries
                )
            except Exception:
                already_exists = False
            if not already_exists:
                failures.append({
                    "url": getattr(policy, "url", ""),
                    "error": "知识库同步失败",
                })
    logger.info("Knowledge base synced: %s policies added", count)
    return failures


def _send_report_notifications(
    report_path,
    task_id: Optional[str] = None,
    max_retries: int = 2,
) -> tuple[dict, list]:
    """通过统一通知服务发送日报，并返回渠道结果与失败明细。

    任务 ID 在同一报告文件上保持稳定，由 NotificationService 自动拼接
    ``{task_id}:{channel}`` 作为幂等键，既能防止跨渠道唯一键冲突，也能
    防止同一份日报被重复推送。skipped 表示未配置或已发送，不算失败。
    """
    report_file = Path(report_path)
    try:
        content = report_file.read_text(encoding="utf-8")
    except Exception as exc:
        error = f"读取报告失败: {exc}"
        logger.error(error)
        return {}, [{
            "channel": "notification_service",
            "notifier": "notification_service",
            "error": error,
        }]

    channels = [notifier.name for notifier in get_enabled_notifiers()]
    if not channels:
        logger.info("没有启用且已配置的通知渠道，跳过日报推送")
        return {}, []

    stable_task_id = task_id or f"daily_report:{report_file.stem}"
    results = NotificationService.notify_channels(
        channels=channels,
        content=content,
        metadata={
            "task_id": stable_task_id,
            "task_type": "daily_report",
            "trigger_type": "scheduled",
            "title": f"政策趋势日报 - {report_file.stem}",
        },
        max_retries=max_retries,
    )

    failures = []
    for channel, item in results.items():
        status = item.get("status") if isinstance(item, dict) else None
        if status == "failed":
            error = item.get("error") or item.get("reason") or "通知发送失败"
            failures.append({
                "channel": channel,
                "notifier": channel,
                "error": error,
            })
        elif status not in {"success", "skipped"}:
            failures.append({
                "channel": channel,
                "notifier": channel,
                "error": f"未知通知状态: {status}",
            })

    logger.info("日报通知结果 task_id=%s: %s", stable_task_id, results)
    return results, failures


def _collect_local_paths() -> list[Path]:
    data_dir = Path("data")
    if not data_dir.exists():
        return []
    return sorted([p for p in data_dir.glob("*.json")] + [p for p in data_dir.glob("*.html")] + [p for p in data_dir.glob("*.htm")])


def _import_local_files(paths: list[Path | str], source_name: str = "国务院", days: Optional[int] = None) -> list:
    """从本地 JSON/HTML 文件导入，支持日期筛选"""
    from bs4 import BeautifulSoup
    from app.scrapers.base import ScrapedItem

    items = []
    seen_urls = set()
    cutoff = (datetime.now() - timedelta(days=days)).date() if days else None

    for p in paths:
        fp = Path(p)
        if not fp.exists():
            continue
        try:
            if fp.suffix.lower() == ".json":
                items.extend(_parse_json_file(fp, source_name, cutoff, seen_urls))
            elif fp.suffix.lower() in (".html", ".htm"):
                html = fp.read_text(encoding="utf-8", errors="replace")
                soup = BeautifulSoup(html, "lxml")
                items.extend(_parse_html_file(soup, fp, source_name, cutoff, seen_urls))
        except Exception as e:
            logger.warning("导入本地文件失败: %s - %s", fp, e)
    return items


def _parse_json_file(fp: Path, source_name: str, cutoff: Optional[datetime], seen_urls: set) -> List[ScrapedItem]:
    from app.scrapers.base import ScrapedItem
    import json as _json
    items = []
    try:
        data = _json.loads(fp.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("JSON 解析失败: %s - %s", fp, e)
        return items
    if not isinstance(data, list):
        return items
    for entry in data:
        url = (entry.get("URL") or "").strip()
        title = (entry.get("TITLE") or "").strip()
        pub_str = (entry.get("DOCRELPUBTIME") or "").strip()
        if not url or not title:
            continue
        if cutoff and pub_str:
            try:
                pub_date = datetime.strptime(pub_str[:10], "%Y-%m-%d")
                if pub_date.date() < cutoff:
                    continue
            except Exception:
                pass
        if not url.startswith("http"):
            url = "https://www.gov.cn" + url if url.startswith("/") else "https://www.gov.cn/" + url
        if url in seen_urls:
            continue
        seen_urls.add(url)
        items.append(ScrapedItem(title=title, url=url, source_name=source_name, government_level="国家", department=source_name))
    return items


def _parse_html_file(soup: BeautifulSoup, fp: Path, source_name: str, cutoff: Optional[datetime], seen_urls: set) -> List[ScrapedItem]:
    from app.scrapers.base import ScrapedItem
    items = []
    count = 0
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        title = a.get_text(" ", strip=True)
        if "/zhengce/" not in href and "/zfwj/" not in href and "/xxgk/" not in href or not title or len(title) < 5:
            continue
        if not href.startswith("http"):
            href = "https://www.gov.cn" + href if href.startswith("/") else "https://www.gov.cn/" + href
        if href in seen_urls:
            continue
        # 提取日期并应用过滤
        pub_time = _extract_date_from_html(soup, href)
        if cutoff and pub_time and pub_time.date() < cutoff:
            continue
        seen_urls.add(href)
        items.append(ScrapedItem(title=title, url=href, source_name=source_name, government_level="国家", department=source_name, publish_time=pub_time))
        count += 1
    return items



def _dedupe_and_save_policies(db, raw_items: list) -> list:
    """在统一入库边界清洗、校验并按规范化 URL 去重。

    单条脏数据失败不应回滚整批。将数据转换且在独立
    savepoint 中写入，保证一条异常不会污染同批其他政策。
    """
    new_policies = []
    seen_keys = set()

    # 一次加载库内链接，避免循环逐条查询。历史 URL
    # 仍保留原样，但比较时统一使用规范化键。
    for (existing_url,) in db.query(Policy.url).all():
        key = normalize_url_for_dedupe(existing_url)
        if key:
            seen_keys.add(key)

    for item in raw_items:
        raw_url = normalize_text(getattr(item, "url", None))
        url_key = normalize_url_for_dedupe(raw_url)
        if not raw_url or not url_key or url_key in seen_keys:
            continue

        title = clean_title(getattr(item, "title", None))
        if not title:
            logger.warning("跳过缺少有效标题的政策: %s", raw_url)
            continue

        try:
            publish_time = getattr(item, "publish_time", None)
            if isinstance(publish_time, str):
                publish_time = datetime.fromisoformat(publish_time)
            if not isinstance(publish_time, datetime):
                publish_time = extract_date_from_url(raw_url)

            policy = Policy(
                title=title,
                url=raw_url,
                department=normalize_text(getattr(item, "department", None)),
                source_name=normalize_text(getattr(item, "source_name", None)),
                government_level=normalize_text(
                    getattr(item, "government_level", None)
                ),
                city=normalize_text(getattr(item, "city", None)),
                # 无法从可信来源确定日期时保存 NULL，不用当前
                # 时间伪造发布时间。
                publish_time=publish_time,
                content=normalize_text(getattr(item, "content", None)),
                summary=normalize_text(getattr(item, "summary", None)),
                keywords=normalize_text(getattr(item, "keywords", None)),
            )
            with db.begin_nested():
                db.add(policy)
                db.flush()
            new_policies.append(policy)
            seen_keys.add(url_key)
        except Exception as exc:
            logger.warning("政策入库失败，已隔离该条数据 %s: %s", raw_url, exc)
            continue

    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    return new_policies


def _select_policies_needing_analysis(db, raw_items: list) -> list:
    """选择本轮采集命中且尚无分析结果的库内政策。

    入库去重和待分析选择必须使用同一套规范化 URL 语义；否则不同协议、
    主机大小写或追踪参数会被去重拦截，却无法被再次选中分析。
    """
    target_keys = {
        normalize_url_for_dedupe(getattr(item, "url", None))
        for item in raw_items
    }
    target_keys.discard("")
    if not target_keys:
        return []

    candidates = (
        db.query(Policy)
        .outerjoin(Analysis, Analysis.policy_id == Policy.id)
        .filter(Analysis.id.is_(None))
        .order_by(Policy.id)
        .all()
    )
    selected = []
    selected_keys = set()
    for policy in candidates:
        key = normalize_url_for_dedupe(policy.url)
        if key in target_keys and key not in selected_keys:
            selected.append(policy)
            selected_keys.add(key)
    return selected


def _failure_detail(policy, error: str, **extra) -> dict:
    """构造统一的部分失败明细，供 CLI/调度器/调用方展示和重试。"""
    detail = {
        "url": getattr(policy, "url", ""),
        "title": getattr(policy, "title", ""),
        "error": str(error),
    }
    detail.update(extra)
    return detail


def _fill_content_and_analyze_with_failures(db, policies: list) -> tuple[list, dict]:
    """补齐正文并分析，同时返回可观测的部分失败明细。

    正文抓取失败和 AI 分析失败不再只写日志：调用方需要知道哪条政策没有
    进入报告，以及失败发生在长时间抓取、外部模型还是持久化阶段。
    """
    policies_with_analysis = []
    failures = {
        "content_extract": [],
        "analysis": [],
    }
    sources = get_enabled_sources()
    for policy in policies:
        content_error = None
        content = getattr(policy, "content", None)
        if not content or len(content.strip()) < 100:
            matched = [s for s in sources if s.get("name") == policy.source_name]
            if not matched:
                content_error = (
                    f"未找到匹配的采集器配置: {policy.source_name}"
                )
            else:
                try:
                    scraper = create_scraper(matched[0])
                    content = scraper.extract_content(policy.url)
                    if content:
                        policy.content = content[:10000]
                        logger.info(
                            "正文提取成功: %s (%s 字符)",
                            policy.title[:30],
                            len(content),
                        )
                    else:
                        content_error = "正文提取返回空"
                except Exception as exc:
                    content_error = f"正文提取异常: {exc}"
                    logger.warning(
                        "正文提取异常: %s - %s", policy.title[:20], exc
                    )
                    import traceback
                    logger.debug(traceback.format_exc())

        current_content = getattr(policy, "content", None)
        if not current_content or len(current_content.strip()) < 50:
            detail = content_error or "无正文或正文过短"
            length = len(current_content) if current_content else 0
            failures["content_extract"].append(
                _failure_detail(policy, detail, content_length=length)
            )
            logger.warning(
                "跳过分析（%s）: %s, 内容长度: %s",
                detail,
                policy.title[:30],
                length,
            )
            continue

        try:
            analysis_result = analyze_policy(policy.content, policy.title)
        except Exception as exc:
            failures["analysis"].append(
                _failure_detail(policy, f"AI 分析调用异常: {exc}")
            )
            logger.error("AI 分析调用异常: %s - %s", policy.title[:20], exc)
            continue

        clean_result = prepare_trusted_analysis(analysis_result)
        if clean_result is None:
            source = (
                analysis_result.get("_analysis_source")
                if isinstance(analysis_result, dict) else None
            )
            status = (
                analysis_result.get("_analysis_status")
                if isinstance(analysis_result, dict) else None
            )
            reason = (
                analysis_result.get("_analysis_error")
                if isinstance(analysis_result, dict) else None
            ) or "分析结果不可信"
            failures["analysis"].append(
                _failure_detail(
                    policy,
                    reason,
                    analysis_source=source,
                    analysis_status=status,
                )
            )
            logger.warning(
                "非可信分析结果不落库、不进入报告: %s (source=%s, status=%s)",
                policy.title[:30],
                source,
                status,
            )
            continue

        # 字段映射、合法 JSON 和 upsert 语义统一由 analysis_persistence 负责。
        # 单条持久化异常必须隔离在当前政策内；否则一条脏数据或瞬时 ORM
        # 故障会中断整批分析，后续政策永远得不到分析机会。
        try:
            analysis = upsert_analysis(db, policy, clean_result)
        except Exception as exc:
            failures["analysis"].append(
                _failure_detail(policy, f"分析结果持久化异常: {exc}")
            )
            logger.error(
                "分析结果持久化异常: %s - %s",
                policy.title[:30],
                exc,
            )
            continue
        if analysis is None:
            failures["analysis"].append(
                _failure_detail(policy, "分析结果持久化失败")
            )
            logger.warning("分析结果持久化失败: %s", policy.title[:30])
            continue

        policies_with_analysis.append({
            "policy": policy,
            "policy_id": policy.id,
            "title": policy.title,
            "url": policy.url,
            "publish_time": policy.publish_time,
            "analysis": clean_result,
        })
        logger.info(
            "分析成功: %s (评分 %s)",
            policy.title[:30],
            clean_result.get("趋势评分", 0),
        )
    return policies_with_analysis, failures


def _fill_content_and_analyze(db, policies: list) -> list:
    """兼容旧调用方：只返回成功分析列表。"""
    policies_with_analysis, _ = _fill_content_and_analyze_with_failures(
        db, policies
    )
    return policies_with_analysis
