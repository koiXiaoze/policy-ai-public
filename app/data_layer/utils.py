"""
Data Layer 通用工具函数
避免数据层反向依赖 Agent 层
"""

from typing import Optional
import re


def build_region_filter(region: str):
    """构建地区过滤条件：精确匹配 + 模糊匹配兜底（解决数据库中文乱码导致的查询失效）"""
    from sqlalchemy import or_
    from app.models.policy import Policy
    return or_(
        Policy.government_level == region,
        Policy.city == region,
        Policy.source_name == region,
        Policy.department == region,
        Policy.government_level.contains(region),
        Policy.city.contains(region),
        Policy.source_name.contains(region),
        Policy.department.contains(region),
    )


def parse_date_range(date_range: str) -> Optional[int]:
    """解析日期范围字符串为天数"""
    date_range = date_range.lower()
    if "today" in date_range or "今天" in date_range:
        return 1
    if "week" in date_range or "周" in date_range or "7" in date_range:
        return 7
    # "近3个月"/"近三个月"等：按 30*N 天处理，避免被单月规则截成30天
    months_match = re.search(r"(\d+|一|两|二|三|四|五|六)\s*个?月", date_range)
    if months_match:
        cn = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6}
        token = months_match.group(1)
        n = cn.get(token) or int(token)
        return min(n * 30, 365)
    if "quarter" in date_range or "季度" in date_range or "90" in date_range:
        return 90
    if "month" in date_range or "月" in date_range or "30" in date_range:
        return 30
    if "year" in date_range or "年" in date_range or "365" in date_range:
        return 365
    match = re.search(r"(\d+)", date_range)
    if match:
        return int(match.group(1))
    return None
