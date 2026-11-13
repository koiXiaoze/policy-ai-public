"""
Agent 任务规划器
将用户自然语言转换为结构化执行计划
"""

from typing import Dict, List, Any, Optional
from dataclasses import dataclass
import json

from app.config import settings
from app.logger import logger

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


PLANNER_PROMPT = """你是一个政策分析 Agent 的任务规划器。

用户会输入自然语言查询，你需要：
1. 理解用户意图
2. 提取关键参数：地区、关键词、时间范围、分析目标
3. 制定执行计划，决定调用哪些工具以及顺序

可用工具：
- search_policies: 搜索政策（地区、关键词、时间范围）
- get_policy_detail: 获取政策详情（需要 policy_id）
- search_historical_policies: 搜索历史政策用于趋势分析
- analyze_policy: AI 深度分析政策全文
- analyze_policy_trend: 对关键词进行历史趋势分析
- generate_report: 基于当前结果生成报告
- collect_new_policies: 触发爬虫采集最新政策

输出格式（JSON）：
{
  "intent": "用户意图分类：search/analyze/trend/report/collect",
  "parameters": {
    "region": "地区",
    "keyword": "关键词",
    "date_range": "时间范围",
    "goal": "分析目标：如产业趋势、就业机会、投资方向"
  },
  "plan": [
    {"step": 1, "tool": "search_policies", "reason": "搜索相关政策"},
    {"step": 2, "tool": "analyze_policy", "reason": "深度分析高分政策"}
  ],
  "needs_detail": true,
  "needs_trend": false
}
"""


@dataclass
class Plan:
    """执行计划"""
    intent: str
    parameters: Dict[str, Any]
    plan: List[Dict[str, Any]]
    needs_detail: bool = False
    needs_trend: bool = False
    raw_response: str = ""


class Planner:
    """任务规划器"""
    
    def __init__(self):
        self.client = None
        if OpenAI and settings.ai_api_key:
            self.client = OpenAI(
                api_key=settings.ai_api_key,
                base_url=settings.ai_api_base,
                timeout=60.0
            )
    
    #: 可用规则直接构造计划的明确模式（省去 5-8 秒 LLM 规划）
    _RULE_PATTERNS = (
        ("分析", ("分析", "解读")),
        ("搜索", ("有哪些", "有什么", "最近", "最新", "政策")),
    )

    def _try_rule_plan(self, user_query: str) -> Optional[Plan]:
        """对高确定性查询用规则直接构造计划，跳过 LLM 规划。

        这些模式不需要 LLM 判断：包含地区+主题的"查政策"和"分析政策"
        是系统最核心的两类请求，规则化后单次省 5-8 秒。
        """
        text = (user_query or "").strip()
        if len(text) < 4 or len(text) > 40:
            return None

        region = None
        for r in ("国务院", "湖北省", "湖北", "武汉市", "武汉", "十堰市", "十堰", "人社部"):
            if r in text:
                region = r
                break
        if not region:
            return None

        wants_analyze = any(k in text for k in ("分析", "解读", "详细说说", "展开讲讲"))
        plan_steps = [
            {"step": 1, "tool": "search_policies", "reason": "搜索相关政策"},
            {"step": 2, "tool": "get_policy_detail", "reason": "获取政策全文"},
        ]
        if wants_analyze:
            plan_steps.append({"step": 3, "tool": "analyze_policy", "reason": "深度分析"})
        logger.info("使用规则计划（跳过 LLM 规划）: analyze=%s region=%s", wants_analyze, region)
        return Plan(
            intent="analyze" if wants_analyze else "search",
            parameters={"region": region, "keyword": "", "date_range": None,
                        "goal": "政策分析" if wants_analyze else "政策查询"},
            plan=plan_steps,
            needs_detail=True,
            raw_response="rule-plan",
        )

    def parse_intent(self, user_query: str, context: Optional[List[Dict[str, str]]] = None) -> Plan:
        """解析用户意图并生成计划"""
        # 省略式追问依赖上下文，不走规则计划
        if context and len((user_query or "").strip()) <= 8:
            pass
        else:
            rule_plan = self._try_rule_plan(user_query)
            if rule_plan is not None:
                return rule_plan

        if not self.client:
            return self._fallback_plan(user_query, context=context)
        
        try:
            response = self.client.chat.completions.create(
                model=settings.ai_model,
                messages=[{"role": "system", "content": PLANNER_PROMPT}]
                + self._normalize_context(context)
                + [{"role": "user", "content": user_query}],
                temperature=0.3,
                response_format={"type": "json_object"}
            )
            raw = response.choices[0].message.content
            data = json.loads(raw)
            parameters = data.get("parameters", {})
            if isinstance(parameters, dict):
                for key, value in parameters.items():
                    if isinstance(value, list):
                        parameters[key] = "、".join(str(item) for item in value)
            
            plan = Plan(
                intent=data.get("intent", "search"),
                parameters=parameters,
                plan=data.get("plan", []),
                needs_detail=data.get("needs_detail", False),
                needs_trend=data.get("needs_trend", False),
                raw_response=raw
            )
            logger.info(f"规划完成: intent={plan.intent}, steps={len(plan.plan)}")
            return plan
        except Exception as e:
            logger.error(f"规划失败: {e}")
            return self._fallback_plan(user_query, context=context)

    def _fallback_plan(
        self,
        user_query: str,
        context: Optional[List[Dict[str, str]]] = None,
    ) -> Plan:
        """兜底规划：基于关键词简单匹配"""
        query = user_query.lower()
        
        # 提取地区
        region = None
        for r in ["国务院", "国家", "湖北", "武汉", "十堰", "人社部", "人力资源"]:
            if r in user_query:
                region = r
                break
        
        # 提取关键词
        keyword = None
        keywords = ["人工智能", "AI", "数字经济", "新能源", "先进制造", "网络安全",
                    "数据安全", "低空经济", "机器人", "生物医药", "新材料",
                    "就业", "教育", "住房", "医疗", "养老", "人才", "社保"]
        for k in keywords:
            if k in user_query:
                keyword = k
                break
        
        # 提取时间范围
        date_range = None
        if "今天" in query or "今日" in query:
            date_range = "今天"
        elif "周" in query or "7天" in query or "一周" in query:
            date_range = "最近一周"
        elif "月" in query or "30天" in query or "一个月" in query:
            date_range = "最近一个月"
        elif "季度" in query or "90天" in query or "三个月" in query:
            date_range = "最近一季度"
        elif "年" in query or "365天" in query or "一年" in query:
            date_range = "最近一年"
        
        # 判断意图
        intent = "search"
        needs_detail = False
        needs_trend = False
        goal = "政策搜索"
        
        if "趋势" in query or "变化" in query or "历史" in query:
            intent = "trend"
            needs_trend = True
            goal = "趋势分析"
        elif "分析" in query or "解读" in query or "影响" in query:
            intent = "analyze"
            needs_detail = True
            goal = "深度分析"
        elif "报告" in query or "日报" in query or "周报" in query:
            intent = "report"
            goal = "生成报告"
        elif "采集" in query or "爬取" in query or "更新" in query:
            intent = "collect"
            goal = "采集新政策"
        
        # 构建计划
        plan_steps = []
        if intent == "collect":
            plan_steps.append({"step": 1, "tool": "collect_new_policies", "reason": "采集最新政策"})
        else:
            plan_steps.append({"step": 1, "tool": "search_policies", "reason": "搜索相关政策"})
            if needs_detail:
                plan_steps.append({"step": 2, "tool": "analyze_policy", "reason": "深度分析政策"})
            if needs_trend:
                plan_steps.append({"step": 2, "tool": "search_historical_policies", "reason": "获取历史政策用于趋势分析"})
        
        plan = Plan(
            intent=intent,
            parameters={
                "region": region,
                "keyword": keyword,
                "date_range": date_range,
                "goal": goal
            },
            plan=plan_steps,
            needs_detail=needs_detail,
            needs_trend=needs_trend
        )
        self._inherit_context_parameters(plan, context)
        return plan

    @staticmethod
    def _normalize_context(context: Optional[List[Dict[str, str]]]) -> List[Dict[str, str]]:
        """只保留受控长度的历史对话，供规划器理解省略表达。"""
        if not context:
            return []

        normalized: List[Dict[str, str]] = []
        for item in context[-10:]:
            role = item.get("role") if isinstance(item, dict) else None
            content = item.get("content") if isinstance(item, dict) else None
            if role in {"user", "assistant"} and isinstance(content, str) and content.strip():
                normalized.append({"role": role, "content": content[:2000]})
        return normalized

    def _inherit_context_parameters(
        self,
        plan: Plan,
        context: Optional[List[Dict[str, str]]],
    ) -> None:
        """无 LLM 时，从同一会话较早的用户问题继承省略的地区、关键词和时间。"""
        user_history = [
            item.get("content", "")
            for item in self._normalize_context(context)
            if item["role"] == "user"
        ]
        if not user_history:
            return

        prior = self._fallback_plan("\n".join(user_history), context=None)
        for key in ("region", "keyword", "date_range"):
            if not plan.parameters.get(key) and prior.parameters.get(key):
                plan.parameters[key] = prior.parameters[key]


# 全局规划器实例
planner = Planner()

print("Planner initialized")
