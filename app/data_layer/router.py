"""
Data Router
智能路由：根据查询类型决定使用哪个数据源
"""

from typing import List, Dict, Any, Optional
from enum import Enum
from dataclasses import dataclass

from app.data_layer.source import (
    SourceType, PolicySource, source_registry, PolicyItem
)
from app.logger import logger


class QueryType(str, Enum):
    REALTIME = "realtime"      # 需要最新数据
    HISTORICAL = "historical"  # 历史趋势分析
    SPECIFIC = "specific"      # 查询特定政策
    GENERAL = "general"        # 通用搜索


@dataclass
class RouteDecision:
    query_type: QueryType
    primary_sources: List[SourceType]
    fallback_sources: List[SourceType]
    reason: str


class DataRouter:
    """数据路由器：判断查询类型，选择最优数据源组合"""
    
    # 权威发布主体：命中后优先走官方采集接口（与CLI采集逻辑一致）
    AUTHORITY_REGION_KEYWORDS = [
        "国务院", "中央", "国家", "国办",
        "人社部", "人力资源", "社会保障",
        "湖北", "武汉", "十堰",
    ]
    
    REALTIME_KEYWORDS = [
        "今天", "今日", "最新", "刚刚", "刚发布", "新政策", "最新政策",
        "今天发布", "今日发布", "刚出台", "新出台"
    ]
    
    HISTORICAL_KEYWORDS = [
        "趋势", "变化", "历史", "过去", "回顾", "演变", "发展",
        "对比", "对比过", "对比一下", "几年", "过去一年", "过去三年",
        "纵向", "横向", "阶段性", "阶段"
    ]
    
    SPECIFIC_KEYWORDS = [
        "具体", "详情", "全文", "原文", "解读", "分析", "深度"
    ]
    
    def __init__(self):
        pass
    
    def route(self, query: str, region: str = None, keyword: str = None) -> RouteDecision:
        """根据查询内容决定路由策略"""
        query_lower = query.lower()
        
        # 0. 权威发布主体查询：官方采集接口优先（gov.cn JSON / 省市门户列表页）
        # region 与完整查询文本都要检查，防止规划器把主体塞进 keyword
        authority_text = f"{region or ''} {query or ''}"
        if any(kw in authority_text for kw in self.AUTHORITY_REGION_KEYWORDS):
            return RouteDecision(
                query_type=QueryType.REALTIME,
                primary_sources=[SourceType.OFFICIAL_API],
                fallback_sources=[SourceType.LOCAL_DB, SourceType.WEB_SEARCH],
                reason=f"检测到权威发布主体「{region}」，优先官方采集接口，本地DB/Web搜索补充"
            )
        
        # 1. 实时查询判断
        if any(kw in query_lower for kw in self.REALTIME_KEYWORDS):
            return RouteDecision(
                query_type=QueryType.REALTIME,
                primary_sources=[SourceType.OFFICIAL_API, SourceType.WEB_SEARCH],
                fallback_sources=[SourceType.LOCAL_DB],
                reason="检测到实时性关键词，优先官方API+Web搜索"
            )
        
        # 2. 历史趋势判断
        if any(kw in query_lower for kw in self.HISTORICAL_KEYWORDS):
            return RouteDecision(
                query_type=QueryType.HISTORICAL,
                primary_sources=[SourceType.LOCAL_DB, SourceType.WEB_SEARCH],
                fallback_sources=[SourceType.OFFICIAL_API],
                reason="检测到趋势/历史关键词，优先本地DB+Web搜索补充"
            )
        
        # 3. 特定政策详情判断
        if any(kw in query_lower for kw in self.SPECIFIC_KEYWORDS) and keyword:
            return RouteDecision(
                query_type=QueryType.SPECIFIC,
                primary_sources=[SourceType.LOCAL_DB, SourceType.OFFICIAL_API],
                fallback_sources=[SourceType.WEB_SEARCH],
                reason="查询特定政策详情，优先本地DB+官方API"
            )
        
        # 4. 通用搜索：数据库优先，Web搜索补充
        return RouteDecision(
            query_type=QueryType.GENERAL,
            primary_sources=[SourceType.LOCAL_DB],
            fallback_sources=[SourceType.WEB_SEARCH, SourceType.OFFICIAL_API],
            reason="通用查询，本地DB优先，Web搜索补充"
        )
    
    def execute_search(self, decision: RouteDecision, region: str = None, keyword: str = None, 
                       date_range: str = None, limit: int = 20) -> List[PolicyItem]:
        """执行搜索，按优先级尝试数据源"""
        all_results = []
        seen_urls = set()
        url_index: Dict[str, int] = {}

        def collect(results: List[PolicyItem]) -> int:
            """合并数据源结果，URL 去重；重复时保留信息更完整的那条。"""
            added = 0
            for item in results:
                idx = url_index.get(item.url)
                if idx is None:
                    url_index[item.url] = len(all_results)
                    seen_urls.add(item.url)
                    all_results.append(item)
                    added += 1
                else:
                    all_results[idx] = self._merge_item(all_results[idx], item)
            return added
        
        # 先尝试主数据源
        for source_type in decision.primary_sources:
            source = source_registry.get_by_type(source_type)
            if not source:
                continue
            
            try:
                results = source.search(region=region, keyword=keyword, date_range=date_range, limit=limit)
                logger.info(f"DataRouter: {source.name} returned {len(results)} results (新增 {collect(results)} 条)")
            except Exception as e:
                logger.warning(f"DataRouter: {source.name} failed: {e}")
        
        # 如果主源结果不足，尝试备用源
        if len(all_results) < limit // 2:
            for source_type in decision.fallback_sources:
                source = source_registry.get_by_type(source_type)
                if not source:
                    continue
                
                try:
                    results = source.search(region=region, keyword=keyword, date_range=date_range, limit=limit - len(all_results))
                    logger.info(f"DataRouter fallback: {source.name} returned {len(results)} results (新增 {collect(results)} 条)")
                except Exception as e:
                    logger.warning(f"DataRouter fallback: {source.name} failed: {e}")
        
        return all_results[:limit]

    @staticmethod
    def _merge_item(existing: PolicyItem, incoming: PolicyItem) -> PolicyItem:
        """同一 URL 命中多个数据源时合并字段。

        官方采集接口只解析列表页，返回的 PolicyItem 没有正文；而本地数据库里
        同一篇政策往往带着已抓取的正文。若按"先到先得"去重，正文会被丢掉，
        导致下游 analyze_policy 拿不到 policy_text。这里以正文为准做取舍。
        """
        def has_body(item: PolicyItem) -> bool:
            return bool(item.content and item.content.strip())

        if has_body(incoming) and not has_body(existing):
            # 进来的版本有正文，直接采用，但保留已有版本里 incoming 缺失的字段
            for field in ("summary", "keywords", "importance_score"):
                if not getattr(incoming, field, None) and getattr(existing, field, None):
                    setattr(incoming, field, getattr(existing, field))
            if not incoming.raw_data and existing.raw_data:
                incoming.raw_data = existing.raw_data
            return incoming

        if has_body(existing):
            # 已有版本有正文，只把新版本补充的字段回填进去
            for field in ("content", "summary", "keywords", "department"):
                if not getattr(existing, field, None) and getattr(incoming, field, None):
                    setattr(existing, field, getattr(incoming, field))
            if not existing.raw_data and incoming.raw_data:
                existing.raw_data = incoming.raw_data
        return existing


# 全局路由器实例
data_router = DataRouter()

print("DataRouter initialized")
