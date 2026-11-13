"""
Agent 工具注册表与工具实现
封装现有模块为 Agent 可调用的工具
- Tool 层只调用 Service 层，Service 层负责底层 Scraper / Database / Analyzer / Reporter
"""

from typing import Any, Dict, List, Optional, Callable
from dataclasses import dataclass
from app.logger import logger
from app.agent.services import PolicyService, ReportService


@dataclass
class Tool:
    """工具定义"""
    name: str
    description: str
    parameters: Dict[str, Any]
    function: Callable


class ToolRegistry:
    """工具注册表"""
    def __init__(self):
        self._tools: Dict[str, Tool] = {}
    
    def register(self, tool: Tool):
        self._tools[tool.name] = tool
    
    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)
    
    def list_tools(self) -> List[Tool]:
        return list(self._tools.values())
    
    def get_function_schemas(self) -> List[Dict[str, Any]]:
        """获取 OpenAI Function Calling 格式的工具定义"""
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters
                }
            }
            for tool in self._tools.values()
        ]


# 全局工具注册表
tool_registry = ToolRegistry()


# 统一工具函数：Tool 层只调用 Service 层
def search_policies(region: str = None, keyword: str = None, date_range: str = None, limit: int = 20) -> List[Dict[str, Any]]:
    """搜索政策（统一命名）"""
    return PolicyService.search_policies(region=region, keyword=keyword, date_range=date_range, limit=limit)


def search_policy(region: str = None, keyword: str = None, date_range: str = None, limit: int = 20) -> List[Dict[str, Any]]:
    """搜索政策（旧名兼容）"""
    return PolicyService.search_policies(region=region, keyword=keyword, date_range=date_range, limit=limit)


def get_policy_detail(policy_id: int = None, url: str = None) -> Optional[Dict[str, Any]]:
    """获取政策详情（统一命名，支持ID或URL）"""
    return PolicyService.get_policy_detail(policy_id=policy_id, url=url)


def search_historical_policies(keyword: str, region: str = None, time_range: str = None, limit: int = 50) -> List[Dict[str, Any]]:
    """搜索历史政策（统一命名，用于趋势分析）"""
    return PolicyService.search_historical_policies(keyword=keyword, region=region, time_range=time_range, limit=limit)


def search_history_policy(keyword: str, region: str = None, time_range: str = None, limit: int = 50) -> List[Dict[str, Any]]:
    """搜索历史政策（旧名兼容）"""
    return PolicyService.search_historical_policies(keyword=keyword, region=region, time_range=time_range, limit=limit)


def analyze_policy(policy_text: str, title: str = None) -> Dict[str, Any]:
    """AI 政策分析（统一命名）"""
    return PolicyService.analyze_policy(policy_text=policy_text, title=title)


def analyze_policies_batch(policies: List[Dict[str, Any]] = None,
                           top_n: int = None,
                           concurrency: int = None) -> Dict[str, Any]:
    """一次批量分析排序后的 TOP N 条政策，固定占用一个 Agent 工具步。"""
    return PolicyService.analyze_policies(
        policies=policies or [],
        top_n=top_n,
        concurrency=concurrency,
    )


def analyze_policy_trend(keyword: str, region: str = None, time_range: str = None) -> Dict[str, Any]:
    """趋势分析（统一命名）：先搜索历史政策，再统计趋势"""
    historical = PolicyService.search_historical_policies(keyword=keyword, region=region, time_range=time_range, limit=50)
    if not historical:
        return {"error": "未找到相关历史政策，无法进行趋势分析"}
    return {
        "keyword": keyword,
        "region": region,
        "time_range": time_range,
        "total_policies": len(historical),
        "policies": historical
    }


def collect_new_policies() -> Dict[str, Any]:
    """采集新政策（统一命名）"""
    return PolicyService.collect_new_policies()


def generate_report(state_or_results: Any = None, mode: str = "daily",
                    source_name: str = None, top_n: int = None) -> Dict[str, Any]:
    """生成报告（统一命名）
    
    Args:
        state_or_results: AgentState 或包含 search_results / analysis_results / policy_details 的字典
        mode: daily / weekly / monthly / quarterly
        source_name: 来源名称
        top_n: 正式报告最多展示多少条；None 时读取统一配置
    """
    # 支持直接传入 AgentState 或 dict
    if hasattr(state_or_results, "search_results"):
        policies = state_or_results.search_results or []
        analyses = state_or_results.analysis_results or []
    elif isinstance(state_or_results, dict):
        policies = state_or_results.get("search_results", [])
        analyses = state_or_results.get("analysis_results", [])
    else:
        return {"error": "需要传入 AgentState 或包含 search_results/analysis_results 的字典"}
    
    if not policies:
        return {"error": "没有可用政策，无法生成报告"}

    # 分级推送：未推送政策进入主区；已推送政策保留为背景参考。
    # 这样既避免重复推送主内容，又不会因为无新增而让报告退化为失败文案。
    from app.services.push_dedup import push_dedup_service
    primary_policies, background_policies = push_dedup_service.split_by_pushed(policies)
    if not primary_policies and not background_policies:
        return {"error": "没有可用政策，无法生成报告"}

    path = ReportService.generate_report(
        policies=primary_policies,
        analyses=analyses,
        mode=mode,
        source_name=source_name,
        background_policies=background_policies,
        top_n=top_n,
    )
    return {
        "success": True,
        "report_path": str(path),
        "pushed_policies": primary_policies,
        "background_count": len(background_policies),
    }


# 注册所有工具
tool_registry.register(Tool(
    name="search_policies",
    description="根据地区、关键词、时间范围搜索政策（多数据源：本地DB优先，Web搜索/官方API补充；内置Filter Layer：来源验证、时间过滤、类型分类、智能排序）",
    parameters={
        "type": "object",
        "properties": {
            "region": {"type": "string", "description": "地区，如：湖北、武汉、国务院"},
            "keyword": {"type": "string", "description": "关键词，如：人工智能、就业、新能源"},
            "date_range": {"type": "string", "description": "时间范围，如：今天、最近一周、最近一个月、最近一季度"},
            "limit": {"type": "integer", "description": "返回数量限制", "default": 20}
        },
        "required": []
    },
    function=search_policy
))

tool_registry.register(Tool(
    name="search_policy",
    description="搜索政策（旧名兼容，建议使用 search_policies）",
    parameters={
        "type": "object",
        "properties": {
            "region": {"type": "string", "description": "地区"},
            "keyword": {"type": "string", "description": "关键词"},
            "date_range": {"type": "string", "description": "时间范围"},
            "limit": {"type": "integer", "description": "返回数量限制", "default": 20}
        },
        "required": []
    },
    function=search_policy
))

tool_registry.register(Tool(
    name="get_policy_detail",
    description="获取政策完整内容和分析结果（支持ID或URL）",
    parameters={
        "type": "object",
        "properties": {
            "policy_id": {"type": "integer", "description": "政策ID（本地数据库）"},
            "url": {"type": "string", "description": "政策URL（跨数据源查询）"}
        },
        "required": []
    },
    function=get_policy_detail
))

tool_registry.register(Tool(
    name="search_historical_policies",
    description="搜索历史政策，用于趋势分析（知识库混合检索：关键词+向量；内置时间过滤、类型分类、智能排序）",
    parameters={
        "type": "object",
        "properties": {
            "keyword": {"type": "string", "description": "关键词"},
            "region": {"type": "string", "description": "地区"},
            "time_range": {"type": "string", "description": "时间范围"},
            "limit": {"type": "integer", "description": "返回数量限制", "default": 50}
        },
        "required": ["keyword"]
    },
    function=search_historical_policies
))

tool_registry.register(Tool(
    name="analyze_policy",
    description="对政策全文进行 AI 深度分析",
    parameters={
        "type": "object",
        "properties": {
            "policy_text": {"type": "string", "description": "政策全文内容"},
            "title": {"type": "string", "description": "政策标题"}
        },
        "required": ["policy_text"]
    },
    function=analyze_policy
))

tool_registry.register(Tool(
    name="analyze_policies_batch",
    description="一次分析排序结果前 TOP N 条政策；候选政策由当前 Agent 状态注入，单条失败不影响其他条目",
    parameters={
        "type": "object",
        "properties": {
            "top_n": {"type": "integer", "description": "最多分析多少条政策", "minimum": 1, "maximum": 10},
            "concurrency": {"type": "integer", "description": "AI 分析并发数", "minimum": 1, "maximum": 5, "default": 3}
        },
        "required": []
    },
    # policies 由 Agent 执行时注入；保留默认值便于直接调用与函数签名反射。
    function=analyze_policies_batch
))

tool_registry.register(Tool(
    name="analyze_policy_trend",
    description="对关键词进行历史政策趋势分析",
    parameters={
        "type": "object",
        "properties": {
            "keyword": {"type": "string", "description": "关键词"},
            "region": {"type": "string", "description": "地区"},
            "time_range": {"type": "string", "description": "时间范围"}
        },
        "required": ["keyword"]
    },
    function=analyze_policy_trend
))

tool_registry.register(Tool(
    name="generate_report",
    description="基于当前搜索/分析结果生成政策报告",
    parameters={
        "type": "object",
        "properties": {
            "mode": {"type": "string", "description": "daily / weekly / monthly / quarterly", "default": "daily"},
            "source_name": {"type": "string", "description": "来源名称"},
            "top_n": {"type": "integer", "description": "报告最多展示的政策条数", "minimum": 1, "maximum": 10}
        },
        "required": []
    },
    # 同时兼容两个入参名：Agent 执行期注入 state_or_results（需要 AgentState 才能汇总），
    # 旧的直接调用方传 results（普通列表/字典）。优先取 state_or_results。
    function=lambda mode="daily", source_name=None, top_n=None, state_or_results=None, results=None: generate_report(
        state_or_results=state_or_results if state_or_results is not None else results,
        mode=mode,
        source_name=source_name,
        top_n=top_n,
    )
))

tool_registry.register(Tool(
    name="collect_new_policies",
    description="触发爬虫采集最新政策",
    parameters={
        "type": "object",
        "properties": {},
        "required": []
    },
    function=collect_new_policies
))

print("Tools registered successfully (with Service Layer)")
