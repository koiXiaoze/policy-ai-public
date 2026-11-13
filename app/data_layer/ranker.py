"""
Result Ranking System
结果排序系统：综合评分并排序
"""

from typing import List, Dict, Any, Optional
from dataclasses import dataclass

from app.data_layer.source_validator import source_validator, SourceScore, is_listing_page
from app.data_layer.classifier import policy_classifier, ClassificationResult
from app.data_layer.date_filter import date_filter, DateFilterResult


@dataclass
class RankingResult:
    """排序结果"""
    item: Dict[str, Any]
    final_score: float
    source_score: SourceScore
    classification: ClassificationResult
    date_filter: DateFilterResult
    score_breakdown: Dict[str, float]


class ResultRanker:
    """结果排序器"""
    
    # 权重配置
    WEIGHTS = {
        "source": 0.4,      # 来源权威性
        "time": 0.3,        # 时间匹配
        "keyword": 0.2,     # 关键词匹配
        "policy_type": 0.1, # 政策类型
    }

    # 时间敏感查询：用户明确关注近期/本周/今天时启用严格时间约束
    TIME_SENSITIVE_RANGES = {"today", "recent_days", "this_week", "last_week"}

    def __init__(self):
        pass

    def _is_time_sensitive(self, time_range) -> bool:
        """判断当前查询是否为时间敏感场景"""
        range_value = getattr(time_range, "value", time_range)
        return range_value in self.TIME_SENSITIVE_RANGES
    
    def rank(self, items: List[Dict[str, Any]], query: str = "", 
             time_range=None, custom_start=None, custom_end=None) -> List[RankingResult]:
        """对结果列表进行排序"""
        if not items:
            return []
        
        # 计算时间范围
        start, end = self._get_time_bounds(time_range, custom_start, custom_end)
        time_sensitive = self._is_time_sensitive(time_range)
        
        ranked = []
        for item in items:
            # 列表页/搜索页/栏目页不是具体政策文档，直接剔除
            if is_listing_page(item.get("url", "")):
                continue

            # 1. 来源评分
            url = item.get("url") or ""
            title = item.get("title") or ""
            content = (item.get("content") or item.get("summary") or "")
            source_score = source_validator.validate(url, title, content)
            
            # 2. 类型分类
            classification = policy_classifier.classify(title, content, url)
            
            # 3. 时间过滤
            date_result = date_filter.filter(item, start, end)
            
            # 时间过滤命中但原始数据无 publish_time 时回写（来源多为URL/标题日期推断）
            # 保证下游Format层能显示真实发布日期而非"日期未知"
            if date_result.matched_date and not item.get("publish_time"):
                item["publish_time"] = date_result.matched_date.isoformat()
            
            # 4. 关键词匹配
            keyword_score = self._calculate_keyword_score(item, query)
            
            # 5. 综合评分
            breakdown = {
                "source": source_score.score * self.WEIGHTS["source"],
                "time": self._time_score(date_result, time_sensitive) * self.WEIGHTS["time"],
                "keyword": keyword_score * self.WEIGHTS["keyword"],
                "policy_type": self._policy_type_score(classification) * self.WEIGHTS["policy_type"],
            }
            
            final_score = sum(breakdown.values())
            
            ranked.append(RankingResult(
                item=item,
                final_score=round(final_score, 2),
                source_score=source_score,
                classification=classification,
                date_filter=date_result,
                score_breakdown=breakdown
            ))
        
        # 按最终得分降序排序
        ranked.sort(key=lambda x: x.final_score, reverse=True)
        
        # 附加排名信息到原item
        for i, result in enumerate(ranked):
            result.item["rank"] = i + 1
            result.item["final_score"] = result.final_score
            result.item["score_breakdown"] = result.score_breakdown
            result.item["source_authority"] = result.source_score.authority.value
            result.item["source_confidence"] = result.source_score.confidence
            result.item["policy_type"] = result.classification.policy_type.value
            result.item["policy_type_confidence"] = result.classification.confidence
            result.item["date_matched"] = result.date_filter.matched_date.isoformat() if result.date_filter.matched_date else None
        
        return ranked
    
    def _get_time_bounds(self, time_range, custom_start, custom_end):
        """获取时间边界"""
        import types
        from app.data_layer.query_parser import query_parser
        
        if time_range and hasattr(time_range, 'value'):
            # 如果是 TimeRange enum
            if time_range.value == "custom":
                return custom_start or datetime.now(), custom_end or datetime.now()
            # SimpleNamespace 避免类体作用域无法读取外层局部变量的问题
            temp_query = types.SimpleNamespace(
                time_range=time_range,
                custom_start=custom_start,
                custom_end=custom_end,
            )
            return query_parser.get_date_bounds(temp_query)
        return datetime.now() - timedelta(days=30), datetime.now()
    
    def _time_score(self, date_result: DateFilterResult, time_sensitive: bool = False) -> float:
        """时间评分 0-100"""
        if not date_result.matched_date:
            # 无法确定日期：时间敏感查询下大幅降权，否则给中等分
            return 10 if time_sensitive else 50
        if date_result.passed:
            return 100
        return 0
    
    def _policy_type_score(self, classification: ClassificationResult) -> float:
        """政策类型评分 0-100"""
        type_scores = {
            "policy_document": 100,
            "meeting": 70,
            "interpretation": 50,
            "announcement": 30,
            "news": 20,
            "commentary": 10,
            "other": 0,
        }
        return type_scores.get(classification.policy_type.value, 0) * classification.confidence
    
    def _calculate_keyword_score(self, item: Dict[str, Any], query: str) -> float:
        """关键词匹配评分 0-100"""
        if not query:
            return 50

        title = (item.get("title") or "").lower()
        content = ((item.get("content") or "") or (item.get("summary") or "")).lower()
        text = title + " " + content

        # 提取查询关键词并扩展同义词，避免相近表述漏匹配
        from app.data_layer.query_parser import query_parser
        base_terms = query_parser._extract_keywords(query)
        query_words = list(query_parser.expand_synonyms(base_terms))
        # 兜底：领域词不足时再按空格分词补充
        if not query_words:
            query_words = [w for w in query.split() if len(w) > 1]
        if not query_words:
            return 50
        
        matches = 0
        for word in query_words:
            if word in title:
                matches += 2
            elif word in content:
                matches += 1
        
        # 归一化
        max_possible = len(query_words) * 2
        if max_possible == 0:
            return 50
        
        return min(100, (matches / max_possible) * 100)
    
    def filter_and_rank(self, items: List[Dict[str, Any]], 
                        query: str = "",
                        time_range=None,
                        custom_start=None,
                        custom_end=None,
                        policy_only: bool = False,
                        min_score: float = 30) -> List[Dict[str, Any]]:
        """过滤并排序"""
        ranked = self.rank(items, query, time_range, custom_start, custom_end)
        
        # 应用过滤
        filtered = []
        for result in ranked:
            # 最低分过滤
            if result.final_score < min_score:
                continue
            
            # 高置信度新闻/评论不是政策文件，政策检索场景直接剔除
            if result.classification.policy_type.value in ("news", "commentary") \
                    and result.classification.confidence >= 0.4:
                continue
            
            # 政策文件过滤
            if policy_only and result.classification.policy_type.value != "policy_document":
                continue
            
            # 时间过滤（严格模式）
            if time_range and not result.date_filter.passed:
                continue
            
            # 时间敏感查询：日期完全无法确定的结果直接排除，
            # 防止历史政策/未知日期内容冒充"本周最新"
            if self._is_time_sensitive(time_range) and result.date_filter.source == "none":
                continue
            
            filtered.append(result.item)

        # 输出严格按发布时间倒序（日期未知的垫底），不再按综合评分排序
        def _dt(item):
            t = item.get("publish_time")
            if isinstance(t, datetime):
                return t
            if isinstance(t, str):
                try:
                    return datetime.fromisoformat(t[:10])
                except ValueError:
                    pass
            return datetime.min

        filtered.sort(key=_dt, reverse=True)
        return filtered


from datetime import datetime, timedelta

# 全局排序器实例
result_ranker = ResultRanker()
