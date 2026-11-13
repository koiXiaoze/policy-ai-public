"""
Date Range Filter
严格的时间范围过滤
"""

import re
from typing import Optional, Tuple, List
from datetime import datetime, timedelta
from dataclasses import dataclass

from app.data_layer.query_parser import TimeRange


@dataclass
class DateFilterResult:
    """时间过滤结果"""
    passed: bool
    matched_date: Optional[datetime]
    source: str  # title/publish_time/content
    reason: str


class DateRangeFilter:
    """时间范围过滤器"""
    
    # 标题中常见日期模式
    TITLE_DATE_PATTERNS = [
        r"(\d{4})年(\d{1,2})月(\d{1,2})日",
        r"(\d{4})-(\d{1,2})-(\d{1,2})",
        r"(\d{4})/(\d{1,2})/(\d{1,2})",
        r"(\d{4})(\d{2})(\d{2})",
    ]

    # URL 中常见的日期模式（政府网站普遍在URL中编码发布日期）
    # 例如：/t20250904_、/2026-09/、/2026/09/04/、/20260904/、/202609/content_7080188.htm
    URL_DATE_PATTERNS = [
        r"/t(\d{4})(\d{2})(\d{2})[_-]",        # 政府站常用：/t20250904_
        r"/(\d{4})/(\d{2})-(\d{2})/",           # /2026/09-04/ 变体
        r"/(\d{4})-(\d{2})-(\d{2})",            # /2026-09-04
        r"/(\d{4})(\d{2})(\d{2})/",             # /20260904/
        r"/(\d{4})/(\d{2})/(\d{2})/",           # /2026/09/04/
        r"/(\d{4})-(\d{2})/",                   # /2026-09/
        r"/(\d{4})/(\d{2})/",                   # /2026/09/
        r"/(\d{4})(\d{2})/content_",            # gov.cn：/202609/content_7080188.htm
        r"/(\d{4})(\d{2})/",                    # 湖北等门户：/202609/W020260901....pdf（精确到月）
    ]
    
    # 正文中日期模式
    CONTENT_DATE_PATTERNS = [
        r"发布时间[：:]\s*(\d{4}[-年]\d{1,2}[-月]\d{1,2}[日]?)",
        r"发布日期[：:]\s*(\d{4}[-年]\d{1,2}[-月]\d{1,2}[日]?)",
        r"公布日期[：:]\s*(\d{4}[-年]\d{1,2}[-月]\d{1,2}[日]?)",
        r"(\d{4})年(\d{1,2})月(\d{1,2})日\s*发布",
        r"(\d{4}-\d{2}-\d{2})\s*发布",
    ]
    
    def __init__(self):
        pass
    
    def filter(self, item: dict, start: datetime, end: datetime) -> DateFilterResult:
        """
        过滤单条结果
        优先级：publish_time > 标题日期 > 正文日期
        """
        # 1. 优先使用 publish_time 字段
        publish_time = item.get("publish_time")
        if publish_time:
            if isinstance(publish_time, str):
                try:
                    publish_time = datetime.fromisoformat(publish_time.replace("Z", "+00:00"))
                except Exception:
                    publish_time = None
            
            if publish_time and start <= publish_time <= end:
                return DateFilterResult(
                    passed=True,
                    matched_date=publish_time,
                    source="publish_time",
                    reason=f"发布时间 {publish_time.date()} 在范围内"
                )
            elif publish_time:
                return DateFilterResult(
                    passed=False,
                    matched_date=publish_time,
                    source="publish_time",
                    reason=f"发布时间 {publish_time.date()} 不在范围内"
                )
        
        # 2. 从URL提取日期（政府站点URL含日期的情况非常普遍）
        url = item.get("url", "") or ""
        url_date = self._extract_date_from_url(url)
        if url_date and start <= url_date <= end:
            return DateFilterResult(
                passed=True,
                matched_date=url_date,
                source="url",
                reason=f"URL日期 {url_date.date()} 在范围内"
            )
        elif url_date:
            return DateFilterResult(
                passed=False,
                matched_date=url_date,
                source="url",
                reason=f"URL日期 {url_date.date()} 不在范围内"
            )
        
        # 3. 从标题提取日期
        title = item.get("title", "")
        title_date = self._extract_date_from_text(title)
        if title_date and start <= title_date <= end:
            return DateFilterResult(
                passed=True,
                matched_date=title_date,
                source="title",
                reason=f"标题日期 {title_date.date()} 在范围内"
            )
        elif title_date:
            return DateFilterResult(
                passed=False,
                matched_date=title_date,
                source="title",
                reason=f"标题日期 {title_date.date()} 不在范围内"
            )
        
        # 4. 从正文/摘要提取日期
        content = item.get("content", "") or item.get("summary", "")
        content_date = self._extract_date_from_text(content)
        if content_date and start <= content_date <= end:
            return DateFilterResult(
                passed=True,
                matched_date=content_date,
                source="content",
                reason=f"正文日期 {content_date.date()} 在范围内"
            )
        elif content_date:
            return DateFilterResult(
                passed=False,
                matched_date=content_date,
                source="content",
                reason=f"正文日期 {content_date.date()} 不在范围内"
            )
        
        # 5. 无法确定日期，默认通过（由排序层按时间敏感查询降权或排除）
        return DateFilterResult(
            passed=True,
            matched_date=None,
            source="none",
            reason="无法提取日期，默认通过"
        )

    def _extract_date_from_url(self, url: str) -> Optional[datetime]:
        """从URL路径中提取日期。

        精确到日的模式返回真实日期；只精确到「年月」的模式
        （如 gov.cn 的 /202610/content_xxx.htm）返回 None，
        交由 publish_time 字段判断。把这些 URL 一律当成当月 1 号，
        会在"最近 N 天"窗口里把当月新政策误判为过期。
        """
        if not url:
            return None
        for pattern in self.URL_DATE_PATTERNS:
            match = re.search(pattern, url)
            if not match:
                continue
            try:
                groups = match.groups()
                year = int(groups[0])
                month = int(groups[1])
                if len(groups) < 3 or groups[2] is None:
                    # 只有年月，无法判断具体发布日期，不能当作月初误杀
                    continue
                day = int(groups[2])
                if 2000 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31:
                    return datetime(year, month, day)
            except (ValueError, IndexError):
                continue
        return None
    
    def _extract_date_from_text(self, text: str) -> Optional[datetime]:
        """从文本中提取日期"""
        if not text:
            return None
        
        # 尝试各种模式
        for pattern in self.TITLE_DATE_PATTERNS:
            match = re.search(pattern, text)
            if match:
                try:
                    groups = match.groups()
                    if len(groups) == 3:
                        return datetime(int(groups[0]), int(groups[1]), int(groups[2]))
                except Exception:
                    pass
        
        for pattern in self.CONTENT_DATE_PATTERNS:
            match = re.search(pattern, text)
            if match:
                try:
                    date_str = match.group(1)
                    # 统一格式
                    date_str = date_str.replace("年", "-").replace("月", "-").replace("日", "")
                    return datetime.strptime(date_str[:10], "%Y-%m-%d")
                except Exception:
                    pass
        
        return None
    
    def filter_batch(self, items: List[dict], time_range: TimeRange, 
                     custom_start: Optional[datetime] = None,
                     custom_end: Optional[datetime] = None) -> List[dict]:
        """批量过滤"""
        from app.data_layer.query_parser import query_parser
        
        # 计算时间范围
        if time_range == TimeRange.CUSTOM:
            start = custom_start or datetime.now()
            end = custom_end or datetime.now()
        else:
            start, end = query_parser.get_date_bounds(
                type("obj", (), {"time_range": time_range, "custom_start": custom_start, "custom_end": custom_end})()
            )
        
        filtered = []
        for item in items:
            result = self.filter(item, start, end)
            item["date_filter"] = {
                "passed": result.passed,
                "matched_date": result.matched_date.isoformat() if result.matched_date else None,
                "source": result.source,
                "reason": result.reason
            }
            if result.passed:
                filtered.append(item)
        
        return filtered


# 全局过滤器实例
date_filter = DateRangeFilter()
