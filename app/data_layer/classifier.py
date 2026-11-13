"""
Policy Type Classifier
政策类型分类器：区分政策文件、新闻、解读、会议等
"""

import re
from typing import Dict, List, Optional
from dataclasses import dataclass
from enum import Enum


class PolicyType(Enum):
    """政策内容类型"""
    POLICY_DOCUMENT = "policy_document"    # 政策文件（条例、办法、规定、通知、意见、决定）
    MEETING = "meeting"                     # 会议（常务会、全会、专题会）
    INTERPRETATION = "interpretation"      # 解读（权威解读、问答、一图读懂）
    NEWS = "news"                           # 新闻报道
    COMMENTARY = "commentary"              # 评论观点
    ANNOUNCEMENT = "announcement"           # 公告通知（人事、招聘、招标等）
    OTHER = "other"                         # 其他


@dataclass
class ClassificationResult:
    """分类结果"""
    policy_type: PolicyType
    confidence: float      # 0-1
    matched_keywords: List[str]
    reasons: List[str]


class PolicyTypeClassifier:
    """政策类型分类器"""
    
    # 各类型特征词（按优先级排序）
    TYPE_PATTERNS = {
        PolicyType.POLICY_DOCUMENT: {
            "strong": [
                r"条例", r"办法", r"规定", r"通知", r"意见", r"决定",
                r"指导意见", r"实施方案", r"行动计划", r"若干措施",
                r"管理办法", r"实施细则", r"实施办法", r"工作方案",
                r"关于.*印发", r"关于.*发布", r"关于.*施行",
            ],
            "medium": [
                r"政策", r"文件", r"标准", r"规范", r"要求",
                r"改革", r"优化", r"调整", r"修订", r"废止",
            ],
            "weak": [
                r"印发", r"发布", r"施行", r"执行", r"落实",
            ],
        },
        PolicyType.MEETING: {
            "strong": [
                r"常务会议", r"全体会议", r"专题会议", r"部署会",
                r"听取.*汇报", r"审议.*通过",
            ],
            "medium": [
                r"会议", r"会上", r"会议指出", r"会议要求",
            ],
            "weak": [],
        },
        PolicyType.INTERPRETATION: {
            "strong": [
                r"权威解读", r"政策解读", r"一图读懂",
                r"问答", r"解读.*政策", r"解读.*文件",
            ],
            "medium": [
                r"解读", r"宣讲", r"培训", r"宣传",
            ],
            "weak": [],
        },
        PolicyType.NEWS: {
            "strong": [
                r"报道", r"新闻", r"消息", r"发布会",
                r"记者.*从.*获悉", r"记者.*采访",
            ],
            "medium": [
                r"媒体", r"发布", r"宣布", r"称",
            ],
            "weak": [],
        },
        PolicyType.COMMENTARY: {
            "strong": [
                r"评论", r"观点", r"专家.*认为", r"学者.*建议",
                r"建议", r"呼吁", r"应当", r"应该",
            ],
            "medium": [
                r"分析", r"研判", r"展望", r"预测",
            ],
            "weak": [],
        },
        PolicyType.ANNOUNCEMENT: {
            "strong": [
                r"公告", r"公示", r"招聘", r"招标", r"采购",
                r"人事任免", r"任命", r"免职",
            ],
            "medium": [
                r"通知.*招聘", r"通知.*考试", r"通知.*报名",
            ],
            "weak": [],
        },
    }
    
    # 排除关键词（如果匹配到这些，降低对应类型置信度）
    EXCLUDE_PATTERNS = {
        PolicyType.POLICY_DOCUMENT: [
            r"解读", r"报道", r"新闻", r"评论", r"专家", r"观点",
            r"一图读懂", r"问答", r"宣讲", r"培训",
        ],
        PolicyType.MEETING: [
            r"解读", r"报道", r"新闻",
        ],
        PolicyType.INTERPRETATION: [
            r"新闻", r"报道", r"发布会",
        ],
    }

    def __init__(self):
        # 编译正则
        self.compiled_patterns = {}
        for ptype, patterns in self.TYPE_PATTERNS.items():
            self.compiled_patterns[ptype] = {
                "strong": [re.compile(p) for p in patterns.get("strong", [])],
                "medium": [re.compile(p) for p in patterns.get("medium", [])],
                "weak": [re.compile(p) for p in patterns.get("weak", [])],
            }
        
        self.exclude_compiled = {}
        for ptype, patterns in self.EXCLUDE_PATTERNS.items():
            self.exclude_compiled[ptype] = [re.compile(p) for p in patterns]

    def classify(self, title: str, content: str = "", url: str = "") -> ClassificationResult:
        """分类单条内容"""
        text = (title + " " + content + " " + url).lower()
        
        scores = {}
        matched_keywords = {}
        
        # 计算各类型得分
        for ptype, patterns in self.compiled_patterns.items():
            score = 0
            matched = []
            
            # 强特征
            for pattern in patterns["strong"]:
                matches = pattern.findall(text)
                if matches:
                    score += 30
                    matched.extend(matches)
            
            # 中特征
            for pattern in patterns["medium"]:
                matches = pattern.findall(text)
                if matches:
                    score += 15
                    matched.extend(matches)
            
            # 弱特征
            for pattern in patterns["weak"]:
                matches = pattern.findall(text)
                if matches:
                    score += 5
                    matched.extend(matches)
            
            # 排除惩罚
            if ptype in self.exclude_compiled:
                for pattern in self.exclude_compiled[ptype]:
                    if pattern.search(text):
                        score -= 20
            
            scores[ptype] = max(0, score)
            matched_keywords[ptype] = matched
        
        # 选择最高分
        if not scores or max(scores.values()) == 0:
            return ClassificationResult(
                policy_type=PolicyType.OTHER,
                confidence=0.1,
                matched_keywords=[],
                reasons=["无明显特征，归类为其他"]
            )
        
        best_type = max(scores, key=scores.get)
        best_score = scores[best_type]
        
        # 归一化置信度
        confidence = min(1.0, best_score / 100.0)
        
        reasons = [f"匹配{len(matched_keywords.get(best_type, []))}个特征词"]
        if best_type in self.exclude_compiled:
            for pattern in self.exclude_compiled[best_type]:
                if pattern.search(text):
                    reasons.append(f"触发排除规则: {pattern.pattern}")
        
        return ClassificationResult(
            policy_type=best_type,
            confidence=round(confidence, 2),
            matched_keywords=matched_keywords.get(best_type, []),
            reasons=reasons
        )
    
    def is_policy_document(self, title: str, content: str = "", url: str = "", 
                           threshold: float = 0.5) -> bool:
        """判断是否为政策文件"""
        result = self.classify(title, content, url)
        return result.policy_type == PolicyType.POLICY_DOCUMENT and result.confidence >= threshold
    
    def filter_policy_only(self, items: List[Dict], threshold: float = 0.5) -> List[Dict]:
        """过滤只要政策文件"""
        filtered = []
        for item in items:
            title = item.get("title", "")
            content = item.get("content", "") or item.get("summary", "")
            url = item.get("url", "")
            
            result = self.classify(title, content, url)
            item["policy_type"] = result.policy_type.value
            item["policy_type_confidence"] = result.confidence
            item["policy_type_matched"] = result.matched_keywords
            
            if result.policy_type == PolicyType.POLICY_DOCUMENT and result.confidence >= threshold:
                filtered.append(item)
        
        return filtered


# 全局分类器实例
policy_classifier = PolicyTypeClassifier()
