"""
Source Authority Validator
验证和评分数据来源权威性
"""

import re
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlparse


class SourceAuthority(Enum):
    """来源权威等级"""
    OFFICIAL_GOV_CN = "official_gov_cn"       # gov.cn 官方
    MINISTRY_OFFICIAL = "ministry_official"    # 部委官网
    PROVINCIAL_OFFICIAL = "provincial_official"  # 省政府官网
    MUNICIPAL_OFFICIAL = "municipal_official"   # 市政府官网
    STATE_MEDIA = "state_media"                # 央媒（新华社、人民日报等）
    LOCAL_MEDIA = "local_media"                # 地方媒体
    COMMERCIAL = "commercial"                  # 商业网站
    UNKNOWN = "unknown"                        # 未知


# 权威机构 → 唯一认可的官方发布域名（硬门禁，严格主机名精确匹配）
# 国务院政策只认中国政府网主库；省/市政策只认本级政府门户主站
AUTHORITY_DOMAIN_GATE = {
    "国务院": {"www.gov.cn"},
    "国务院办公厅": {"www.gov.cn"},
    "人力资源和社会保障部": {"www.mohrss.gov.cn"},
    "湖北省人民政府": {"www.hubei.gov.cn"},
    "武汉市人民政府": {"www.wuhan.gov.cn"},
    "十堰市人民政府": {"www.shiyan.gov.cn"},
}


def passes_authority_domain_gate(url: str, authority: Optional[str]) -> bool:
    """
    权威域名门禁：
    - authority 在门禁表中 → URL主机名必须精确匹配认可域名
    - authority 不在门禁表中 → 不限制（部委等场景暂走评分排序）
    """
    if not authority or authority not in AUTHORITY_DOMAIN_GATE:
        return True
    if not url:
        return False
    host = urlparse(url).netloc.lower()
    return host in AUTHORITY_DOMAIN_GATE[authority]


@dataclass
class SourceScore:
    """来源评分结果"""
    authority: SourceAuthority
    score: int              # 0-100
    is_official: bool       # 是否官方来源
    is_policy_source: bool  # 是否政策发布源
    confidence: float       # 置信度 0-1
    reasons: List[str]      # 评分理由


# 官方域名白名单
OFFICIAL_DOMAINS = {
    # 国务院及直属机构
    "gov.cn": SourceAuthority.OFFICIAL_GOV_CN,
    "www.gov.cn": SourceAuthority.OFFICIAL_GOV_CN,
    "state.gov.cn": SourceAuthority.OFFICIAL_GOV_CN,
    "scopsr.gov.cn": SourceAuthority.OFFICIAL_GOV_CN,
    
    # 国务院办公厅
    "gov.cn": SourceAuthority.OFFICIAL_GOV_CN,
    
    # 部委官网
    "ndrc.gov.cn": SourceAuthority.MINISTRY_OFFICIAL,
    "miit.gov.cn": SourceAuthority.MINISTRY_OFFICIAL,
    "mof.gov.cn": SourceAuthority.MINISTRY_OFFICIAL,
    "mohrss.gov.cn": SourceAuthority.MINISTRY_OFFICIAL,
    "mohurd.gov.cn": SourceAuthority.MINISTRY_OFFICIAL,
    "mot.gov.cn": SourceAuthority.MINISTRY_OFFICIAL,
    "mara.gov.cn": SourceAuthority.MINISTRY_OFFICIAL,
    "mofcom.gov.cn": SourceAuthority.MINISTRY_OFFICIAL,
    "mct.gov.cn": SourceAuthority.MINISTRY_OFFICIAL,
    "nhc.gov.cn": SourceAuthority.MINISTRY_OFFICIAL,
    "mem.gov.cn": SourceAuthority.MINISTRY_OFFICIAL,
    "samr.gov.cn": SourceAuthority.MINISTRY_OFFICIAL,
    "tax.gov.cn": SourceAuthority.MINISTRY_OFFICIAL,
    "csrc.gov.cn": SourceAuthority.MINISTRY_OFFICIAL,
    "nfra.gov.cn": SourceAuthority.MINISTRY_OFFICIAL,
    "cac.gov.cn": SourceAuthority.MINISTRY_OFFICIAL,
    "pbc.gov.cn": SourceAuthority.MINISTRY_OFFICIAL,
    
    # 省级政府官网（湖北）
    "hubei.gov.cn": SourceAuthority.PROVINCIAL_OFFICIAL,
    "www.hubei.gov.cn": SourceAuthority.PROVINCIAL_OFFICIAL,
    
    # 市级政府官网
    "wuhan.gov.cn": SourceAuthority.MUNICIPAL_OFFICIAL,
    "www.wuhan.gov.cn": SourceAuthority.MUNICIPAL_OFFICIAL,
    "shiyan.gov.cn": SourceAuthority.MUNICIPAL_OFFICIAL,
    "www.shiyan.gov.cn": SourceAuthority.MUNICIPAL_OFFICIAL,
    
    # 央媒
    "xinhuanet.com": SourceAuthority.STATE_MEDIA,
    "www.xinhuanet.com": SourceAuthority.STATE_MEDIA,
    "people.com.cn": SourceAuthority.STATE_MEDIA,
    "www.people.com.cn": SourceAuthority.STATE_MEDIA,
    "cctv.com": SourceAuthority.STATE_MEDIA,
    "www.cctv.com": SourceAuthority.STATE_MEDIA,
    "gmw.cn": SourceAuthority.STATE_MEDIA,
    "www.gmw.cn": SourceAuthority.STATE_MEDIA,
    "guangming.com.cn": SourceAuthority.STATE_MEDIA,
    "www.guangming.com.cn": SourceAuthority.STATE_MEDIA,
    "chinanews.com": SourceAuthority.STATE_MEDIA,
    "www.chinanews.com": SourceAuthority.STATE_MEDIA,
}

# 政策文件典型路径特征
POLICY_PATH_PATTERNS = [
    r"/zhengce/",           # 政策
    r"/zfwj/",              # 政府文件
    r"/xxgk/",              # 信息公开
    r"/flfg/",              # 法律法规
    r"/zcfg/",              # 政策法规
    r"/zcwj/",              # 政策文件
    r"/gfxwj/",             # 规范性文件
    r"/content_\d+\.htm",   # gov.cn内容页
]

# 非政策路径特征
NON_POLICY_PATH_PATTERNS = [
    r"/news/",              # 新闻
    r"/media/",             # 媒体
    r"/interview/",         # 访谈
    r"/comment/",           # 评论
    r"/opinion/",           # 观点
    r"/analysis/",          # 分析
    r"/special/",           # 专题
    r"/topic/",             # 专题
    r"/ztzl/",              # 专题专栏
    r"/video/",             # 视频
    r"/live/",              # 直播
]

# 列表页/栏目页/搜索页路径特征（不是具体政策文档，应在结果中剔除）
LISTING_PATH_PATTERNS = [
    r"search",              # 搜索页（search.html、sousuo.gov.cn 等）
    r"/s\.htm",             # gov.cn 搜索页
    r"/zcwjk",              # 政策文件库入口
    r"/zhengcewenjianku$",  # 政策文件库栏目
    r"/zcwdpt$",            # 政策问答平台
    r"/file$",              # 文件列表目录页，如 /xxgk/zd/file
    r"/zc/$",               # 政策栏目
    r"/zwgk/?$",            # 政务公开栏目首页
    r"/index[^/]*\.s?html?$",  # index.html / index_1.shtml 列表页
    # 专题栏目下的固定导航页（如 /zhuanti/2026zt/ggcx/zcfb）
    r"/zcfb$",              # 政策发布栏目
    r"/zcjd$",              # 政策解读栏目
    r"/zcfg$",              # 政策法规栏目
    r"/zcwj$",              # 政策文件栏目
    r"/xxgk/?$",            # 信息公开栏目
]


def is_listing_page(url: str) -> bool:
    """判断URL是否为列表/搜索/栏目页（非具体政策文档页）"""
    if not url:
        return True
    path = urlparse(url).path.lower()
    if len(path.strip("/")) == 0:
        return True  # 纯首页
    if any(re.search(pattern, path) for pattern in LISTING_PATH_PATTERNS):
        return True
    # 政府网站的具体文档页URL必然含日期或文档编号（t20260906、content_7080188等）
    # 路径中没有任何数字序列的短路径（如 /xxgk、/xxgk/zcjd/policy.shtml）视为栏目列表页
    if not re.search(r"\d{4,}", path):
        return True
    return False


class SourceValidator:
    """来源权威性验证器"""
    
    def __init__(self):
        self.domain_scores = OFFICIAL_DOMAINS
    
    def validate(self, url: str, title: str = "", content: str = "") -> SourceScore:
        """验证来源并评分"""
        parsed = urlparse(url)
        domain = parsed.netloc.lower().replace("www.", "")
        
        # 1. 基础域名评分
        authority, base_score = self._get_domain_authority(domain)
        
        # 2. 路径加分/减分
        path_score = self._score_path(parsed.path)
        
        # 3. 标题内容特征
        content_score = self._score_content(title, content)
        
        # 4. 综合评分
        final_score = min(100, max(0, base_score + path_score + content_score))
        
        # 5. 判断是否官方/政策源
        is_official = authority in [
            SourceAuthority.OFFICIAL_GOV_CN,
            SourceAuthority.MINISTRY_OFFICIAL,
            SourceAuthority.PROVINCIAL_OFFICIAL,
            SourceAuthority.MUNICIPAL_OFFICIAL
        ]
        
        is_policy_source = is_official and path_score > 0
        
        # 6. 置信度
        confidence = self._calculate_confidence(final_score, is_official, path_score)
        
        # 7. 生成理由
        reasons = self._generate_reasons(authority, path_score, content_score, is_official)
        
        return SourceScore(
            authority=authority,
            score=final_score,
            is_official=is_official,
            is_policy_source=is_policy_source,
            confidence=confidence,
            reasons=reasons
        )
    
    def _get_domain_authority(self, domain: str) -> Tuple[SourceAuthority, int]:
        """获取域名权威等级和基础分"""
        # 精确匹配
        if domain in self.domain_scores:
            auth = self.domain_scores[domain]
            scores = {
                SourceAuthority.OFFICIAL_GOV_CN: 100,
                SourceAuthority.MINISTRY_OFFICIAL: 80,
                SourceAuthority.PROVINCIAL_OFFICIAL: 70,
                SourceAuthority.MUNICIPAL_OFFICIAL: 60,
                SourceAuthority.STATE_MEDIA: 40,
                SourceAuthority.LOCAL_MEDIA: 20,
                SourceAuthority.COMMERCIAL: 10,
                SourceAuthority.UNKNOWN: 0,
            }
            return auth, scores[auth]
        
        # 子域名匹配
        for official_domain, auth in self.domain_scores.items():
            if domain.endswith("." + official_domain) or domain == official_domain:
                scores = {
                    SourceAuthority.OFFICIAL_GOV_CN: 100,
                    SourceAuthority.MINISTRY_OFFICIAL: 80,
                    SourceAuthority.PROVINCIAL_OFFICIAL: 70,
                    SourceAuthority.MUNICIPAL_OFFICIAL: 60,
                    SourceAuthority.STATE_MEDIA: 40,
                    SourceAuthority.LOCAL_MEDIA: 20,
                    SourceAuthority.COMMERCIAL: 10,
                    SourceAuthority.UNKNOWN: 0,
                }
                return auth, scores[auth]
        
        # 检查是否政府域名模式
        if domain.endswith(".gov.cn") or domain.endswith(".gov.XX"):
            return SourceAuthority.MUNICIPAL_OFFICIAL, 50
        
        return SourceAuthority.UNKNOWN, 0
    
    def _score_path(self, path: str) -> int:
        """路径评分"""
        score = 0
        path = path or ""
        path_lower = path.lower()
        
        # 政策路径加分
        for pattern in POLICY_PATH_PATTERNS:
            if re.search(pattern, path_lower):
                score += 20
                break
        
        # 非政策路径减分
        for pattern in NON_POLICY_PATH_PATTERNS:
            if re.search(pattern, path_lower):
                score -= 30
                break
        
        return score
    
    def _score_content(self, title: str, content: str) -> int:
        """标题内容评分"""
        score = 0
        text = (title + " " + content).lower()
        
        # 政策文件特征词
        policy_keywords = [
            "条例", "办法", "规定", "通知", "意见", "决定", "指导意见",
            "实施方案", "行动计划", "若干措施", "管理办法", "实施细则",
            "关于", "印发", "发布", "施行", "废止", "修订", "修改",
        ]
        
        # 非政策特征词
        non_policy_keywords = [
            "报道", "新闻", "消息", "发布会", "记者", "采访",
            "专家", "解读", "分析", "评论", "观点", "建议",
            "一图读懂", "问答", "热点", "关注",
        ]
        
        for kw in policy_keywords:
            if kw in text:
                score += 10
                break
        
        for kw in non_policy_keywords:
            if kw in text:
                score -= 15
                break
        
        return max(-20, min(20, score))
    
    def _calculate_confidence(self, score: int, is_official: bool, path_score: int) -> float:
        """计算置信度"""
        base = score / 100.0
        if is_official and path_score > 0:
            base = min(1.0, base + 0.2)
        return round(base, 2)
    
    def _generate_reasons(self, authority: SourceAuthority, path_score: int, 
                          content_score: int, is_official: bool) -> List[str]:
        """生成评分理由"""
        reasons = []
        
        auth_labels = {
            SourceAuthority.OFFICIAL_GOV_CN: "gov.cn官方域名",
            SourceAuthority.MINISTRY_OFFICIAL: "部委官网",
            SourceAuthority.PROVINCIAL_OFFICIAL: "省政府官网",
            SourceAuthority.MUNICIPAL_OFFICIAL: "市政府官网",
            SourceAuthority.STATE_MEDIA: "央媒",
            SourceAuthority.LOCAL_MEDIA: "地方媒体",
            SourceAuthority.COMMERCIAL: "商业网站",
            SourceAuthority.UNKNOWN: "未知来源",
        }
        reasons.append(f"来源: {auth_labels.get(authority, authority.value)}")
        
        if path_score > 0:
            reasons.append("路径符合政策发布目录")
        elif path_score < 0:
            reasons.append("路径疑似非政策内容")
        
        if content_score > 0:
            reasons.append("标题含政策文件特征词")
        elif content_score < 0:
            reasons.append("标题含新闻/解读特征词")
        
        if is_official:
            reasons.append("认定为官方政策发布源")
        
        return reasons
    
    def filter_policy_only(self, items: List[Dict], threshold: int = 50) -> List[Dict]:
        """过滤只要政策文件"""
        filtered = []
        for item in items:
            url = item.get("url", "")
            title = item.get("title", "")
            content = item.get("content", "") or item.get("summary", "")
            
            score_result = self.validate(url, title, content)
            item["source_score"] = score_result.score
            item["source_authority"] = score_result.authority.value
            item["is_policy_source"] = score_result.is_policy_source
            item["source_confidence"] = score_result.confidence
            
            if score_result.score >= threshold:
                filtered.append(item)
        
        return filtered


# 全局验证器实例
source_validator = SourceValidator()
