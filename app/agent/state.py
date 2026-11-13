"""
Agent 状态管理
定义 Agent 执行过程中的状态数据结构
"""

from datetime import datetime
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field
from enum import Enum


class TaskStatus(str, Enum):
    """任务执行状态"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    REQUIRES_MORE_INFO = "requires_more_info"


class ToolCallStatus(str, Enum):
    """工具调用状态"""
    PENDING = "pending"
    EXECUTING = "executing"
    SUCCESS = "success"
    FAILED = "failed"


@dataclass
class ToolCall:
    """单次工具调用记录"""
    name: str
    arguments: Dict[str, Any]
    result: Any = None
    status: ToolCallStatus = ToolCallStatus.PENDING
    error: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class AgentState:
    """Agent 完整状态"""
    task_id: str
    user_query: str
    user_request: Optional[str] = None
    trigger_type: str = "user"
    task_type: str = "user_query"
    # 定时任务的报告周期（daily/weekly/monthly）。
    report_mode: Optional[str] = None
    region: Optional[str] = None
    keyword: Optional[str] = None
    date_range: Optional[str] = None
    # 检索窗口回退覆盖：窄窗口结果不足时，用更宽窗口重试。
    # 由 PolicyAgent 在执行循环里设置，_task_search_date_range 优先读它。
    search_date_range_override: Optional[str] = None
    parsed_intent: Optional[Dict[str, Any]] = None
    status: TaskStatus = TaskStatus.PENDING
    current_step: int = 0
    max_steps: int = 10
    plan: List[Dict[str, Any]] = field(default_factory=list)
    tool_calls: List[ToolCall] = field(default_factory=list)
    search_results: List[Dict[str, Any]] = field(default_factory=list)
    policies: List[Dict[str, Any]] = field(default_factory=list)
    policy_details: List[Dict[str, Any]] = field(default_factory=list)
    # analysis_results 仅保存通过校验的全文分析结果，报告只能消费它。
    analysis_results: List[Dict[str, Any]] = field(default_factory=list)
    analyses: List[Dict[str, Any]] = field(default_factory=list)
    # 标题推测等降级产物保留用于诊断和用户提示，但不得落库或进入正式报告。
    degraded_analyses: List[Dict[str, Any]] = field(default_factory=list)
    # 非字典、显式 error、兜底失败等不可用结果。
    analysis_failures: List[Dict[str, Any]] = field(default_factory=list)
    # 定时任务通过 analyze_policies_batch 一次分析 TOP N，不使用这里的游标。
    # analyze_cursor 仅服务自然语言单条追问：记录已处理到 search_results 的第几条。
    # 不能用 len(analysis_results) 代替：正文过短导致分析失败时不会入列，
    # 游标会一直卡在同一条上反复空转。
    analyze_cursor: int = 0
    # 本次单条 analyze_policy 实际分析的政策，绑定 policy_id/url/title 时以它为准。
    current_analyze_target: Optional[Dict[str, Any]] = None
    historical_data: List[Dict[str, Any]] = field(default_factory=list)
    next_action: Optional[str] = None
    final_answer: Optional[str] = None
    final_result: Optional[str] = None
    report_path: Optional[str] = None
    report_error: Optional[str] = None
    # 省略式追问时复用的上一轮政策主键
    followup_policy_id: Optional[int] = None
    # 本次报告中实际参与推送的政策，供调度器在发送成功后登记去重台账。
    pushed_policies: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

    def add_tool_call(self, name: str, arguments: Dict[str, Any]) -> ToolCall:
        call = ToolCall(name=name, arguments=arguments)
        self.tool_calls.append(call)
        self.updated_at = datetime.now()
        return call

    def update_tool_result(self, tool_call: ToolCall, result: Any, error: Optional[str] = None):
        tool_call.result = result
        tool_call.status = ToolCallStatus.FAILED if error else ToolCallStatus.SUCCESS
        tool_call.error = error
        self.updated_at = datetime.now()

    def get_last_tool_call(self) -> Optional[ToolCall]:
        if self.tool_calls:
            return self.tool_calls[-1]
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "user_query": self.user_query,
            "user_request": self.user_request or self.user_query,
            "trigger_type": self.trigger_type,
            "task_type": self.task_type,
            "report_mode": self.report_mode,
            "region": self.region,
            "keyword": self.keyword,
            "date_range": self.date_range,
            "parsed_intent": self.parsed_intent,
            "status": self.status.value,
            "current_step": self.current_step,
            "plan": self.plan,
            "tool_calls": [
                {
                    "name": tc.name,
                    "arguments": tc.arguments,
                    "status": tc.status.value,
                    "error": tc.error,
                    "timestamp": tc.timestamp.isoformat()
                }
                for tc in self.tool_calls
            ],
            "search_results_count": len(self.search_results),
            "policies_count": len(self.policies),
            "policy_details_count": len(self.policy_details),
            "analysis_results_count": len(self.analysis_results),
            "analyses_count": len(self.analyses),
            "degraded_analyses_count": len(self.degraded_analyses),
            "analysis_failures_count": len(self.analysis_failures),
            "historical_data_count": len(self.historical_data),
            "next_action": self.next_action,
            "final_answer": self.final_answer[:200] if self.final_answer else None,
            "final_result": self.final_result[:200] if self.final_result else None,
            "report_path": self.report_path,
            "report_error": self.report_error,
            "error": self.error,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
