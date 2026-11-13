"""
Policy Query Parser
将自然语言查询转换为结构化查询参数
"""

import re
from typing import Dict, Any, Optional, List
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum


class TimeRange(str, Enum):
    TODAY = "today"
    RECENT_DAYS = "recent_days"
    THIS_WEEK = "this_week"
    LAST_WEEK = "last_week"
    RECENT_MONTH = "recent_month"
    RECENT_QUARTER = "recent_quarter"
    THIS_MONTH = "this_month"
    LAST_MONTH = "last_month"
    THIS_QUARTER = "this_quarter"
    THIS_YEAR = "this_year"
    CUSTOM = "custom"


class AuthorityLevel(str, Enum):
    STATE_COUNCIL = "state_council"      # 国务院
    MINISTRY = "ministry"                 # 部委
    PROVINCIAL = "provincial"             # 省级
    MUNICIPAL = "municipal"               # 市级
    NEWS_MEDIA = "news_media"             # 新闻媒体
    OTHER = "other"


@dataclass
class PolicyQuery:
    """结构化政策查询"""
    original_query: str
    authority: Optional[str] = None           # 权威机构：国务院、湖北省、武汉市
    authority_level: Optional[AuthorityLevel] = None
    keywords: List[str] = None                # 关键词
    time_range: Optional[TimeRange] = None    # 时间范围
    custom_start: Optional[datetime] = None   # 自定义开始时间
    custom_end: Optional[datetime] = None     # 自定义结束时间
    source_required: bool = False             # 是否要求官方来源
    policy_only: bool = False                 # 只要政策文件
    policy_types: List[str] = None            # 政策类型过滤
    intent: str = "search"                    # search/analyze/trend/report
    
    def __post_init__(self):
        if self.keywords is None:
            self.keywords = []
        if self.policy_types is None:
            self.policy_types = []


class PolicyQueryParser:
    """政策查询解析器"""
    
    # 权威机构映射
    AUTHORITY_MAP = {
        "国务院": ("国务院", AuthorityLevel.STATE_COUNCIL),
        "中央": ("国务院", AuthorityLevel.STATE_COUNCIL),
        "国家": ("国务院", AuthorityLevel.STATE_COUNCIL),
        "国办": ("国务院办公厅", AuthorityLevel.STATE_COUNCIL),
        "发改委": ("国家发展改革委", AuthorityLevel.MINISTRY),
        "工信部": ("工业和信息化部", AuthorityLevel.MINISTRY),
        "财政部": ("财政部", AuthorityLevel.MINISTRY),
        "人社部": ("人力资源和社会保障部", AuthorityLevel.MINISTRY),
        "住建部": ("住房和城乡建设部", AuthorityLevel.MINISTRY),
        "交通部": ("交通运输部", AuthorityLevel.MINISTRY),
        "农业部": ("农业农村部", AuthorityLevel.MINISTRY),
        "商务部": ("商务部", AuthorityLevel.MINISTRY),
        "文旅部": ("文化和旅游部", AuthorityLevel.MINISTRY),
        "卫健委": ("国家卫生健康委", AuthorityLevel.MINISTRY),
        "应急部": ("应急管理部", AuthorityLevel.MINISTRY),
        "市监局": ("国家市场监管总局", AuthorityLevel.MINISTRY),
        "税务局": ("国家税务总局", AuthorityLevel.MINISTRY),
        "证监会": ("中国证监会", AuthorityLevel.MINISTRY),
        "银保监会": ("国家金融监管总局", AuthorityLevel.MINISTRY),
        "网信办": ("国家网信办", AuthorityLevel.MINISTRY),
        "湖北": ("湖北省人民政府", AuthorityLevel.PROVINCIAL),
        "湖北省": ("湖北省人民政府", AuthorityLevel.PROVINCIAL),
        "武汉": ("武汉市人民政府", AuthorityLevel.MUNICIPAL),
        "武汉市": ("武汉市人民政府", AuthorityLevel.MUNICIPAL),
        "十堰": ("十堰市人民政府", AuthorityLevel.MUNICIPAL),
        "十堰市": ("十堰市人民政府", AuthorityLevel.MUNICIPAL),
    }
    
    # 时间关键词映射
    TIME_MAP = {
        "今天": TimeRange.TODAY,
        "今日": TimeRange.TODAY,
        "这周": TimeRange.RECENT_DAYS,
        "本周": TimeRange.RECENT_DAYS,
        "最近一周": TimeRange.RECENT_DAYS,
        "最近7天": TimeRange.RECENT_DAYS,
        "近7天": TimeRange.RECENT_DAYS,
        "上周": TimeRange.LAST_WEEK,
        "最近一个月": TimeRange.RECENT_MONTH,
        "近一个月": TimeRange.RECENT_MONTH,
        "近一个季度": TimeRange.RECENT_QUARTER,
        "最近一季度": TimeRange.RECENT_QUARTER,
        "最近一个季度": TimeRange.RECENT_QUARTER,
        "近三个月": TimeRange.RECENT_QUARTER,
        "最近三个月": TimeRange.RECENT_QUARTER,
        "这个月": TimeRange.THIS_MONTH,
        "本月": TimeRange.THIS_MONTH,
        "上个月": TimeRange.LAST_MONTH,
        "本季度": TimeRange.THIS_QUARTER,
        "今年": TimeRange.THIS_YEAR,
        # "最近"系列：政府政策发布稀疏，7 天窗口经常为空，
        # 统一按 30 天理解更符合用户对"最近/近期"的预期。
        # 精确的"最近7天"仍在上面单独映射。
        "最近": TimeRange.RECENT_MONTH,
        "近期": TimeRange.RECENT_MONTH,
        "近来": TimeRange.RECENT_MONTH,
        "近段时间": TimeRange.RECENT_MONTH,
        "这段时间": TimeRange.RECENT_MONTH,
        "最新": TimeRange.RECENT_MONTH,
        "最近发布": TimeRange.RECENT_MONTH,
        "近期发布": TimeRange.RECENT_MONTH,
    }
    
    # 政策类型关键词
    POLICY_TYPE_KEYWORDS = {
        "政策文件": ["条例", "办法", "规定", "通知", "意见", "决定", "指导意见", "实施方案", "行动计划", "若干措施"],
        "会议": ["会议", "常务会", "全会", "专题会", "部署会"],
        "解读": ["解读", "问答", "一图读懂", "权威解读", "政策解读"],
        "新闻": ["新闻", "报道", "消息", "发布会", "媒体"],
        "评论": ["评论", "观点", "分析", "专家", "学者"],
    }
    
    # 意图关键词
    INTENT_KEYWORDS = {
        "search": ["什么", "哪些", "有哪些", "查询", "搜索", "找找"],
        "analyze": ["分析", "解读", "影响", "意义", "深度"],
        "trend": ["趋势", "变化", "走向", "演变", "历史", "对比"],
        "report": ["报告", "日报", "周报", "月报", "总结"],
    }

    # 领域同义词组：用于关键词扩展，避免"不认识"相关表述
    # 每组内任意词命中，则整组都参与匹配（提升召回，同时不跨域误扩）
    SYNONYM_GROUPS = [
        {"就业", "招聘", "失业", "劳动力", "稳岗", "扩岗", "创业"},
        {"社保", "社会保险", "养老保险", "失业保险", "工伤保险", "医保", "生育保险"},
        {"人才", "职称", "职业技能", "技工", "专家", "引才"},
        {"人工智能", "AI", "大模型", "机器学习"},
        {"新能源", "光伏", "风电", "储能", "氢能"},
        {"数字经济", "数字化", "大数据", "算力", "数据要素"},
        {"住房", "公积金", "房地产", "保障性住房", "租房"},
        {"医疗", "医药", "卫生", "公立医院", "集采"},
        {"教育", "高校", "职业教育", "双减", "义务教育"},
        {"汽车", "新能源车", "电动车", "智能网联", "自动驾驶"},
        {"低空经济", "无人机", "通航", "eVTOL"},
        {"机器人", "智能制造", "自动化", "数控机床"},
    ]

    def expand_synonyms(self, terms: List[str]) -> List[str]:
        """将命中词扩展为其同义词组（去重，保留原顺序）"""
        expanded = []
        seen = set()
        for t in terms:
            if t in seen:
                continue
            seen.add(t)
            expanded.append(t)
            for group in self.SYNONYM_GROUPS:
                if t in group:
                    for syn in group:
                        if syn not in seen:
                            seen.add(syn)
                            expanded.append(syn)
                    break
        return expanded

    def parse(self, query: str) -> PolicyQuery:
        """解析自然语言查询"""
        pq = PolicyQuery(original_query=query)
        q_lower = query.lower()
        
        # 1. 解析权威机构（两级匹配：先具体部委/省市，后"国务院/国家"泛词）
        # 避免"国家 人社部"这类组合被泛词抢先命中
        GENERIC_NATION_KEYS = ("国务院", "中央", "国家", "国办")
        for pass_keys in ([k for k in self.AUTHORITY_MAP if k not in GENERIC_NATION_KEYS],
                          GENERIC_NATION_KEYS):
            for keyword in pass_keys:
                if keyword in query:
                    auth, level = self.AUTHORITY_MAP[keyword]
                    pq.authority = auth
                    pq.authority_level = level
                    break
            if pq.authority:
                break
        
        # 2. 解析时间范围
        # 数值天数优先："最近30天"、"近15天" → 自定义区间
        # （须先于此处理，否则"最近30天"会被"最近"词表抢先匹配成最近7天）
        days_match = re.search(r"(?:最近|近|过去)\s*(\d+)\s*天", query)
        if days_match:
            days = min(int(days_match.group(1)), 365)
            now = datetime.now()
            pq.time_range = TimeRange.CUSTOM
            # 窗口边界必须对齐到整秒：历史数据发布时间往往是当天 00:00:00，
            # 若下界保留当前时间的微秒，会把边界日当天发布的数据误判为过期。
            pq.custom_end = now.replace(hour=23, minute=59, second=59, microsecond=0)
            pq.custom_start = (now - timedelta(days=days)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        else:
            for keyword, tr in self.TIME_MAP.items():
                if keyword in query:
                    pq.time_range = tr
                    break
        
        # 3. 解析自定义时间（如：9月1日-9月7日）
        if not pq.time_range or pq.time_range == TimeRange.CUSTOM:
            if not pq.custom_start:
                self._parse_custom_date(query, pq)
        
        # 4. 解析关键词（提取领域词）
        base_keywords = self._extract_keywords(query)
        pq.keywords = self.expand_synonyms(base_keywords)
        
        # 5. 判断是否要求官方来源
        if any(kw in q_lower for kw in ["官方", "原文", "正文", "全文", "gov.cn", "官网"]):
            pq.source_required = True
        
        # 6. 判断是否只要政策文件
        if any(kw in q_lower for kw in ["政策文件", "文件", "条例", "办法", "规定", "通知", "意见"]):
            pq.policy_only = True
        
        # 7. 解析意图
        for intent, keywords in self.INTENT_KEYWORDS.items():
            if any(kw in q_lower for kw in keywords):
                pq.intent = intent
                break
        
        # 8. 提取政策类型过滤
        pq.policy_types = self._extract_policy_types(query)
        
        return pq
    
    def _parse_custom_date(self, query: str, pq: PolicyQuery):
        """解析自定义日期范围"""
        # 匹配：9月1日、2026年9月、2026-09-01 等
        patterns = [
            r"(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})日?",
            r"(\d{1,2})[月/-](\d{1,2})日?",
            r"(\d{4})年(\d{1,2})月",
        ]
        
        dates = []
        for pattern in patterns:
            for match in re.finditer(pattern, query):
                groups = match.groups()
                try:
                    if len(groups) == 3:
                        dates.append(datetime(int(groups[0]), int(groups[1]), int(groups[2])))
                    elif len(groups) == 2:
                        if int(groups[0]) > 12:
                            dates.append(datetime(int(groups[0]), int(groups[1]), 1))
                        else:
                            # 默认当年
                            dates.append(datetime(datetime.now().year, int(groups[0]), int(groups[1])))
                except Exception:
                    pass
        
        if len(dates) >= 2:
            pq.time_range = TimeRange.CUSTOM
            pq.custom_start = min(dates)
            pq.custom_end = max(dates)
        elif len(dates) == 1:
            pq.time_range = TimeRange.CUSTOM
            pq.custom_start = dates[0]
            pq.custom_end = dates[0] + timedelta(days=1)
    
    def _extract_keywords(self, query: str) -> List[str]:
        """提取领域关键词"""
        domain_keywords = [
            "人工智能", "AI", "数字经济", "新能源", "先进制造", "网络安全",
            "数据安全", "低空经济", "机器人", "生物医药", "新材料",
            "就业", "教育", "住房", "医疗", "养老", "人才", "社保",
            "税收", "金融", "贸易", "投资", "招商", "产业园", "高新区",
            "住房公积金", "公积金", "个税", "个税改革", "退税",
        ]
        
        found = []
        for kw in domain_keywords:
            if kw in query:
                found.append(kw)
        return found
    
    def _extract_policy_types(self, query: str) -> List[str]:
        """提取用户想要的政策类型"""
        types = []
        for ptype, keywords in self.POLICY_TYPE_KEYWORDS.items():
            if any(kw in query for kw in keywords):
                types.append(ptype)
        return types
    
    def get_date_bounds(self, pq: PolicyQuery) -> tuple:
        """获取时间范围的具体开始/结束时间"""
        now = datetime.now()
        
        if pq.time_range == TimeRange.RECENT_QUARTER:
            # 最近90天（含今天），区别于自然季度
            start = (now - timedelta(days=89)).replace(hour=0, minute=0, second=0)
            return start, now.replace(hour=23, minute=59, second=59)
        if pq.time_range == TimeRange.TODAY:
            # 过去24小时滚动窗口：适配数据源日期滞后（例如深夜时最新政策仍是昨天发布）
            # start 对齐到零点：政策 publish_time 通常是日期类型（零点），避免秒级误差误杀
            return (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0), now
        elif pq.time_range == TimeRange.RECENT_DAYS:
            # 最近7天（含今天），用于"这周/本周/最近"等口语化表达
            start = (now - timedelta(days=6)).replace(hour=0, minute=0, second=0)
            return start, now.replace(hour=23, minute=59, second=59)
        elif pq.time_range == TimeRange.THIS_WEEK:
            monday = now - timedelta(days=now.weekday())
            return monday.replace(hour=0, minute=0, second=0), (monday + timedelta(days=6)).replace(hour=23, minute=59, second=59)
        elif pq.time_range == TimeRange.LAST_WEEK:
            monday = now - timedelta(days=now.weekday() + 7)
            return monday.replace(hour=0, minute=0, second=0), (monday + timedelta(days=6)).replace(hour=23, minute=59, second=59)
        elif pq.time_range == TimeRange.RECENT_MONTH:
            # 最近30天（含今天），区别于自然月
            start = (now - timedelta(days=29)).replace(hour=0, minute=0, second=0)
            return start, now.replace(hour=23, minute=59, second=59)
        elif pq.time_range == TimeRange.THIS_MONTH:
            start = now.replace(day=1, hour=0, minute=0, second=0)
            if now.month == 12:
                end = now.replace(year=now.year+1, month=1, day=1) - timedelta(seconds=1)
            else:
                end = now.replace(month=now.month+1, day=1) - timedelta(seconds=1)
            return start, end
        elif pq.time_range == TimeRange.LAST_MONTH:
            if now.month == 1:
                start = now.replace(year=now.year-1, month=12, day=1, hour=0, minute=0, second=0)
            else:
                start = now.replace(month=now.month-1, day=1, hour=0, minute=0, second=0)
            end = start.replace(day=1) + timedelta(days=32)
            end = end.replace(day=1) - timedelta(seconds=1)
            return start, end
        elif pq.time_range == TimeRange.THIS_QUARTER:
            quarter = (now.month - 1) // 3 + 1
            start_month = (quarter - 1) * 3 + 1
            start = now.replace(month=start_month, day=1, hour=0, minute=0, second=0)
            end_month = quarter * 3 + 1
            if end_month > 12:
                end = now.replace(year=now.year+1, month=1, day=1) - timedelta(seconds=1)
            else:
                end = now.replace(month=end_month, day=1) - timedelta(seconds=1)
            return start, end
        elif pq.time_range == TimeRange.THIS_YEAR:
            return now.replace(month=1, day=1, hour=0, minute=0, second=0), now.replace(month=12, day=31, hour=23, minute=59, second=59)
        elif pq.time_range == TimeRange.CUSTOM:
            return pq.custom_start, pq.custom_end
        
        # 默认最近一周
        monday = now - timedelta(days=now.weekday())
        return monday.replace(hour=0, minute=0, second=0), now


# 全局解析器实例
query_parser = PolicyQueryParser()


def parse_policy_query(query: str) -> PolicyQuery:
    """便捷函数"""
    return query_parser.parse(query)
