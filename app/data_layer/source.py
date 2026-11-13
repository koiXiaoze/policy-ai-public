"""
Policy Source Adapters
统一的数据源接口：本地DB、Web Search、官方API
"""

from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum


class SourceType(str, Enum):
    LOCAL_DB = "local_db"
    WEB_SEARCH = "web_search"
    OFFICIAL_API = "official_api"


@dataclass
class PolicyItem:
    """统一的政策数据结构"""
    title: str
    url: str
    source_name: str
    government_level: str
    department: Optional[str] = None
    city: Optional[str] = None
    publish_time: Optional[datetime] = None
    content: Optional[str] = None
    summary: Optional[str] = None
    keywords: Optional[str] = None
    importance_score: float = 0.0
    source_type: SourceType = SourceType.LOCAL_DB
    raw_data: Optional[Dict[str, Any]] = None


class PolicySource(ABC):
    """政策数据源抽象基类"""
    
    @property
    @abstractmethod
    def source_type(self) -> SourceType:
        pass
    
    @property
    @abstractmethod
    def name(self) -> str:
        pass
    
    @abstractmethod
    def search(self, region: str = None, keyword: str = None, date_range: str = None, limit: int = 20) -> List[PolicyItem]:
        """搜索政策"""
        pass
    
    @abstractmethod
    def fetch_detail(self, url: str) -> Optional[PolicyItem]:
        """获取政策详情"""
        pass
    
    @abstractmethod
    def get_latest(self, limit: int = 10) -> List[PolicyItem]:
        """获取最新政策"""
        pass
    
    def is_available(self) -> bool:
        """数据源是否可用"""
        return True


class LocalDBSource(PolicySource):
    """本地数据库源"""
    
    @property
    def source_type(self) -> SourceType:
        return SourceType.LOCAL_DB
    
    @property
    def name(self) -> str:
        return "Local Database"
    
    def search(self, region: str = None, keyword: str = None, date_range: str = None, limit: int = 20) -> List[PolicyItem]:
        from app.database import SessionLocal
        from app.models.policy import Policy
        from app.data_layer.utils import build_region_filter, parse_date_range
        
        db = SessionLocal()
        try:
            query = db.query(Policy)
            
            if region:
                query = query.filter(build_region_filter(region))
            
            if keyword:
                query = query.filter(Policy.title.contains(keyword))
            
            if date_range:
                days = parse_date_range(date_range)
                if days:
                    # 下界对齐到当天零点：若保留当前时刻的时分秒，会把边界日
                    # 00:00:00 发布/入库的政策误判为过期（发布时间常为日期零点）。
                    cutoff = (datetime.now() - timedelta(days=days)).replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )
                    query = query.filter(Policy.publish_time >= cutoff)
            
            policies = query.order_by(Policy.publish_time.desc()).limit(limit).all()
            
            results = []
            for p in policies:
                results.append(PolicyItem(
                    title=p.title,
                    url=p.url,
                    source_name=p.source_name,
                    government_level=p.government_level,
                    department=p.department,
                    city=p.city,
                    publish_time=p.publish_time,
                    content=p.content,
                    summary=p.summary,
                    keywords=p.keywords,
                    importance_score=p.importance_score,
                    source_type=SourceType.LOCAL_DB,
                    raw_data={"id": p.id}
                ))
            return results
        finally:
            db.close()
    
    def fetch_detail(self, url: str) -> Optional[PolicyItem]:
        from app.database import SessionLocal
        from app.models.policy import Policy, Analysis
        
        db = SessionLocal()
        try:
            policy = db.query(Policy).filter(Policy.url == url).first()
            if not policy:
                return None
            analysis = db.query(Analysis).filter(Analysis.policy_id == policy.id).first()
            return PolicyItem(
                title=policy.title,
                url=policy.url,
                source_name=policy.source_name,
                government_level=policy.government_level,
                department=policy.department,
                city=policy.city,
                publish_time=policy.publish_time,
                content=policy.content,
                summary=policy.summary,
                keywords=policy.keywords,
                importance_score=policy.importance_score,
                source_type=SourceType.LOCAL_DB,
                raw_data={"id": policy.id, "analysis": {
                    "summary": analysis.summary if analysis else None,
                    "plain_explanation": analysis.plain_explanation if analysis else None,
                    "national_direction": analysis.national_direction if analysis else None,
                    "industry_impact": analysis.industry_impact if analysis else None,
                    "career_opportunity": analysis.career_opportunity if analysis else None,
                    "personal_advice": analysis.personal_advice if analysis else None,
                    "risk_analysis": analysis.risk_analysis if analysis else None,
                    "trend_score": analysis.trend_score if analysis else None,
                } if analysis else None}
            )
        finally:
            db.close()
    
    def get_latest(self, limit: int = 10) -> List[PolicyItem]:
        from app.database import SessionLocal
        from app.models.policy import Policy
        
        db = SessionLocal()
        try:
            policies = db.query(Policy).order_by(Policy.publish_time.desc()).limit(limit).all()
            results = []
            for p in policies:
                results.append(PolicyItem(
                    title=p.title,
                    url=p.url,
                    source_name=p.source_name,
                    government_level=p.government_level,
                    department=p.department,
                    city=p.city,
                    publish_time=p.publish_time,
                    content=p.content,
                    summary=p.summary,
                    keywords=p.keywords,
                    importance_score=p.importance_score,
                    source_type=SourceType.LOCAL_DB,
                    raw_data={"id": p.id}
                ))
            return results
        finally:
            db.close()


class WebSearchSource(PolicySource):
    """Web搜索源（Tavily等）"""
    
    # 地区 → 官方门户域名（与 AUTHORITY_DOMAIN_GATE 对齐）
    # 避免从部委子站、地方融媒等渠道采集"看起来像"的内容
    REGION_DOMAIN_MAP = {
        "国务院": ["www.gov.cn"],
        "中央": ["www.gov.cn"],
        "国家": ["www.gov.cn"],
        "人社部": ["www.mohrss.gov.cn"],
        "人力资源": ["www.mohrss.gov.cn"],
        "社会保障": ["www.mohrss.gov.cn"],
        "湖北": ["www.hubei.gov.cn"],
        "湖北省": ["www.hubei.gov.cn"],
        "武汉": ["www.wuhan.gov.cn"],
        "十堰": ["www.shiyan.gov.cn"],
    }
    
    # 未指定地区时的默认域（只保留四个本级门户主站）
    DEFAULT_DOMAINS = [
        "www.gov.cn", "www.hubei.gov.cn",
        "www.wuhan.gov.cn", "www.shiyan.gov.cn",
    ]
    
    def __init__(self):
        self.tavily_api_key = None
        self._init_tavily()
    
    def _init_tavily(self):
        try:
            from app.config import settings
            if settings.tavily_api_key:
                self.tavily_api_key = settings.tavily_api_key
        except Exception:
            pass
    
    def _domains_for_region(self, region: str = None) -> List[str]:
        """按地区选择搜索域名白名单"""
        if region:
            for key, domains in self.REGION_DOMAIN_MAP.items():
                if key in region:
                    return domains
        return self.DEFAULT_DOMAINS

    @property
    def source_type(self) -> SourceType:
        return SourceType.WEB_SEARCH
    
    @property
    def name(self) -> str:
        return "Tavily Web Search"
    
    def is_available(self) -> bool:
        return self.tavily_api_key is not None
    
    def search(self, region: str = None, keyword: str = None, date_range: str = None, limit: int = 20) -> List[PolicyItem]:
        if not self.is_available():
            return []
        
        try:
            from tavily import TavilyClient
            client = TavilyClient(api_key=self.tavily_api_key)
            
            # 构建查询
            query_parts = []
            if region:
                query_parts.append(region)
            if keyword:
                query_parts.append(keyword)
            query_parts.append("政策")
            query = " ".join(query_parts)
            
            # 时间范围
            days = 30
            if date_range:
                from app.data_layer.utils import parse_date_range
                parsed = parse_date_range(date_range)
                if parsed:
                    days = parsed
            
            response = client.search(
                query=query,
                max_results=limit,
                search_depth="advanced",
                include_domains=self._domains_for_region(region),
                days=days
            )
            
            results = []
            for item in response.get("results", []):
                results.append(PolicyItem(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    source_name=self._extract_source(item.get("url", "")),
                    government_level=self._infer_level(item.get("url", "")),
                    publish_time=self._parse_date(item.get("published_date")),
                    content=item.get("content", "")[:5000],
                    summary=item.get("content", "")[:500],
                    source_type=SourceType.WEB_SEARCH,
                    raw_data=item
                ))
            return results
        except Exception as e:
            from app.logger import logger
            logger.warning(f"Tavily search failed: {e}")
            return []
    
    def fetch_detail(self, url: str) -> Optional[PolicyItem]:
        if not self.is_available():
            return None
        
        try:
            from tavily import TavilyClient
            client = TavilyClient(api_key=self.tavily_api_key)
            
            response = client.extract(urls=[url])
            if response.get("results"):
                item = response["results"][0]
                return PolicyItem(
                    title=item.get("title", ""),
                    url=url,
                    source_name=self._extract_source(url),
                    government_level=self._infer_level(url),
                    content=item.get("raw_content", item.get("content", "")),
                    summary=item.get("content", "")[:500],
                    source_type=SourceType.WEB_SEARCH,
                    raw_data=item
                )
        except Exception as e:
            from app.logger import logger
            logger.warning(f"Tavily extract failed: {e}")
        return None
    
    def get_latest(self, limit: int = 10) -> List[PolicyItem]:
        return self.search(keyword="最新政策", limit=limit)
    
    def _extract_source(self, url: str) -> str:
        # 必须从最具体的域名开始判断，"gov.cn" 是 hubei.gov.cn 等域名的子串
        if "hubei.gov.cn" in url:
            return "湖北省人民政府"
        elif "shiyan.gov.cn" in url:
            return "十堰市人民政府"
        elif "wuhan.gov.cn" in url:
            return "武汉市人民政府"
        elif "mohrss.gov.cn" in url:
            return "人力资源和社会保障部"
        elif "://" in url and url.split("/")[2].endswith(".gov.cn") and not url.split("/")[2].endswith("www.gov.cn"):
            # 其他 *.gov.cn 子站（部委/直属机构），不能直接标为国务院
            return "国务院部委网站"
        if "gov.cn" in url:
            return "国务院"
        return "Web搜索"
    
    def _infer_level(self, url: str) -> str:
        if "hubei.gov.cn" in url:
            return "省"
        elif "shiyan.gov.cn" in url or "wuhan.gov.cn" in url:
            return "市"
        elif "gov.cn" in url:
            return "国家"
        return "未知"
    
    def _parse_date(self, date_str: Optional[str]) -> Optional[datetime]:
        if not date_str:
            return None
        try:
            return datetime.strptime(date_str[:10], "%Y-%m-%d")
        except Exception:
            return None


class OfficialAPISource(PolicySource):
    """
    官方发布源（与CLI采集逻辑一致）
    不再硬编码单个JSON，而是复用 config.yaml 中各来源的官方采集器
    （国务院JSON接口 / 湖北省政府门户列表页 / 十堰市政府列表页等）
    """
    
    @property
    def source_type(self) -> SourceType:
        return SourceType.OFFICIAL_API
    
    @property
    def name(self) -> str:
        return "Official Government APIs"
    
    # 常用别称 → config.yaml 中的官方来源名称（region 归一化用）
    REGION_ALIAS_MAP = {
        "人社部": "人力资源和社会保障部",
        "人力资源": "人力资源和社会保障部",
        "社会保障": "人力资源和社会保障部",
        "国务院": "国务院",
    }

    def _match_sources(self, region: str = None, query: str = None) -> List[Dict]:
        """按地区匹配 config.yaml 中启用的官方来源"""
        from app.config import get_enabled_sources
        sources = get_enabled_sources()
        if not region and not query:
            return sources
        # 归一化：用解析器的权威映射把"国家/人社部"等说法映射到标准主体名
        from app.data_layer.query_parser import query_parser
        text = f"{region or ''} {query or ''}"
        parsed = query_parser.parse(text)
        if parsed.authority:
            candidates = [s for s in sources
                          if parsed.authority in s.get("name", "") or s.get("name", "") in parsed.authority]
            if candidates:
                return candidates
        normalized = region
        for alias, official_name in self.REGION_ALIAS_MAP.items():
            if region and alias in region:
                normalized = official_name
                break
        keys = [normalized, normalized.replace("省", "").replace("市", "")]
        return [s for s in sources if any(k and k in s.get("name", "") for k in keys)]
    
    def search(self, region: str = None, keyword: str = None, date_range: str = None, limit: int = 20) -> List[PolicyItem]:
        from app.scrapers.factory import create_scraper
        from app.logger import logger
        
        days = None
        if date_range:
            from app.data_layer.utils import parse_date_range
            days = parse_date_range(date_range)
        
        results = []
        for cfg in self._match_sources(region, query=keyword):
            try:
                cfg2 = dict(cfg)
                if days:
                    cfg2["days_back"] = days
                scraper = create_scraper(cfg2)
                items = scraper.fetch()
                logger.info(f"官方采集 {cfg2.get('name')}: {len(items)} 条")
                
                if keyword:
                    # keyword 可能是长句（如"近一个季度发布的政策"），拆成领域词后任意命中即可。
                    # 若解析不出已知领域词（如"量子计算"这类未收录主题），
                    # 退化为用原词做子串匹配——否则会跳过过滤、把全量政策
                    # 当成该主题的结果返回，用户会看到大量不相关内容。
                    from app.data_layer.query_parser import query_parser as _qp
                    base = _qp.parse(keyword).keywords
                    kw_terms = _qp.expand_synonyms(base) if base else base
                    kw_terms = [k for k in kw_terms if len(k) >= 2]

                    if not kw_terms:
                        # 原词兜底：去掉空白与常见疑问词后直接匹配标题
                        raw_term = keyword.strip()
                        for noise in ("相关政策", "有关政策", "政策", "有哪些", "有什么", "最近", "相关"):
                            raw_term = raw_term.replace(noise, "")
                        raw_term = raw_term.strip()
                        if len(raw_term) >= 2:
                            kw_terms = [raw_term]

                    if kw_terms:
                        # 词序容错：长关键词（如"行政审批事项优化调整"）与标题
                        # 的实际表述（"优化调整部分行政审批事项"）词序不同，
                        # 严格子串会漏检。这里对长词拆成核心片段，
                        # 要求命中足够比例的片段（默认 60%）。
                        def _title_matches(title: str) -> bool:
                            text = title or ""
                            for term in kw_terms:
                                if term in text:
                                    return True
                                if len(term) >= 6:
                                    fragments = [
                                        frag for frag in _split_core_fragments(term)
                                        if len(frag) >= 2
                                    ]
                                    if not fragments:
                                        continue
                                    hit = sum(1 for frag in fragments if frag in text)
                                    if hit >= max(1, int(len(fragments) * 0.6)):
                                        return True
                            return False

                        items = [i for i in items if _title_matches(i.title)]
                
                for i in items:
                    results.append(PolicyItem(
                        title=i.title,
                        url=i.url,
                        source_name=i.source_name or cfg2.get("name", ""),
                        government_level=i.government_level or cfg2.get("level", ""),
                        department=i.department,
                        city=i.city or cfg2.get("city"),
                        publish_time=i.publish_time,
                        content=i.content,
                        summary=i.summary,
                        source_type=SourceType.OFFICIAL_API,
                    ))
            except Exception as e:
                logger.warning(f"官方采集失败 {cfg.get('name')}: {e}")
        return results[:limit]
    
    def fetch_detail(self, url: str) -> Optional[PolicyItem]:
        # 官方API通常不提供单条详情，回退到Web搜索
        return None
    
    def get_latest(self, limit: int = 10) -> List[PolicyItem]:
        return self.search(limit=limit)


def _split_core_fragments(term: str) -> List[str]:
    """把中文长词拆成核心片段，用于词序容错匹配。

    不引入分词依赖：按常见政策用语边界切分，再补上 2 字滑窗，
    保证"行政审批事项优化调整"能对上"优化调整部分行政审批事项"。
    """
    # 常见政策动宾结构边界词
    BOUNDARIES = (
        "优化调整", "审批事项", "行政审批", "管理规定", "实施办法",
        "若干措施", "行动计划", "实施方案", "指导意见", "发展规划",
    )
    parts: List[str] = []
    remaining = term
    for word in BOUNDARIES:
        if word in remaining:
            parts.append(word)
            remaining = remaining.replace(word, "")

    # 剩余部分按 2-3 字切块
    remaining = remaining.strip()
    if remaining:
        if len(remaining) <= 3:
            parts.append(remaining)
        else:
            for i in range(0, len(remaining) - 1, 2):
                parts.append(remaining[i:i + 2])
    return parts


# 数据源注册表
class SourceRegistry:
    def __init__(self):
        self._sources: List[PolicySource] = []
        self._register_defaults()
    
    def _register_defaults(self):
        self.register(LocalDBSource())
        self.register(WebSearchSource())
        self.register(OfficialAPISource())
    
    def register(self, source: PolicySource):
        self._sources.append(source)
    
    def get_sources(self, source_types: List[SourceType] = None) -> List[PolicySource]:
        if source_types:
            return [s for s in self._sources if s.source_type in source_types and s.is_available()]
        return [s for s in self._sources if s.is_available()]
    
    def get_by_type(self, source_type: SourceType) -> Optional[PolicySource]:
        for s in self._sources:
            if s.source_type == source_type and s.is_available():
                return s
        return None


# 全局注册表
source_registry = SourceRegistry()

print("Data layer sources registered")
