"""
Policy Intelligence Agent 核心
整合规划、工具、记忆、状态管理
输出：用户友好的格式化回答
"""

from typing import Dict, List, Any, Optional
from dataclasses import dataclass
import uuid
import json
import re

from app.logger import logger
from app.agent.state import AgentState, TaskStatus, ToolCallStatus
from app.agent.planner import planner, Plan
from app.agent.tools import tool_registry
from app.agent.services import PolicyService
from app.analyzers.ai_analyzer import (
    ANALYSIS_STATUS_FAILED,
    is_trusted_analysis,
)
from app.config import settings
from app.agent.memory import memory
from app.agent.response_formatter import format_agent_response
from app.models.agent_record import AgentExecutionRecord
from app.models.agent_task import AgentTask
from app.database import SessionLocal
from datetime import datetime


@dataclass
class AgentResult:
    """Agent 执行结果"""
    success: bool
    answer: str              # 用户可见的最终回答（Markdown）
    raw_answer: str = ""     # 原始工具输出（用于调试）
    state: AgentState = None
    error: Optional[str] = None


class ToolExecutionError(RuntimeError):
    """工具执行的基础设施级失败（异常、超时、未知工具）。

    与工具返回的业务错误（例如"政策正文过短，无法分析"）区分开：
    业务错误是正常的计划分支，不应该中断整条定时任务链路；
    只有这里抛出的异常才代表工具真的没跑起来，需要把任务标记为失败。
    """

    def __init__(self, tool_name: str, message: str):
        super().__init__(message)
        self.tool_name = tool_name


class PolicyAgent:
    """政策智能分析 Agent"""
    
    def __init__(self):
        self.max_steps = 10
        self.trigger_type = "user"
    
    def run(
        self,
        user_query: str,
        trigger_type: str = "user",
        task_id: Optional[str] = None,
        task_type: str = "user_query",
        context: Optional[List[Dict[str, str]]] = None,
    ) -> AgentResult:
        """执行用户查询，返回格式化后的用户友好回答。

        context 来自调用方已隔离的会话历史；Agent Core 不自行按渠道读取历史。
        """
        task_id = task_id or str(uuid.uuid4())[:8]
        self.trigger_type = trigger_type
        logger.info(f"[{task_id}] 开始处理: {user_query}")
        
        # 初始化状态
        state = AgentState(
            task_id=task_id,
            user_query=user_query,
            user_request=user_query,
            trigger_type=trigger_type,
            task_type=task_type,
        )
        state.status = TaskStatus.RUNNING
        state.next_action = "解析任务意图"
        # 定时任务模板是报告周期的权威配置；在规划前注入状态。
        if task_type != "user_query":
            try:
                from app.agent.scheduler import TASK_TEMPLATES
                template = TASK_TEMPLATES.get(task_type) or {}
                state.report_mode = template.get("report_mode")
            except Exception as exc:
                logger.warning(f"[{task_id}] 注入报告模式失败: {exc}")
        self._ensure_agent_task(state)
        
        # 旧 CLI 仍使用本地记忆；远程渠道只使用调用方提供的隔离会话上下文，
        # 避免不同飞书用户写入同一份全局 memory 文件。
        use_legacy_memory = trigger_type in {"user", "cli"}
        if use_legacy_memory:
            memory.add("user", user_query)
        
        try:
            # Step 1: 规划
            scheduled_steps = self._scheduled_plan_steps(task_type)
            if scheduled_steps is not None:
                # 定时任务用确定性计划：固定业务链路不该由 LLM 临场决定步骤数。
                plan = Plan(
                    intent="scheduled_report",
                    parameters={},
                    plan=scheduled_steps,
                    raw_response="deterministic",
                )
                logger.info(
                    f"[{task_id}] 定时任务使用确定性计划: {task_type}, "
                    f"steps={len(scheduled_steps)}"
                )
            else:
                plan = planner.parse_intent(user_query, context=context)
            state.parsed_intent = {
                "intent": plan.intent,
                "parameters": plan.parameters,
                "plan": plan.plan,
                "needs_detail": plan.needs_detail,
                "needs_trend": plan.needs_trend
            }
            state.region = plan.parameters.get("region")
            state.keyword = plan.parameters.get("keyword")
            state.date_range = plan.parameters.get("date_range")
            if scheduled_steps is not None:
                # 计划是确定的，检索窗口也应展示任务模板里的真实值
                state.date_range = self._task_search_date_range(state)
            state.plan = plan.plan
            state.next_action = "执行工具计划"
            logger.info(f"[{task_id}] 规划完成: intent={plan.intent}, steps={len(plan.plan)}")

            # 省略式追问（"分析一下"/"详细说说"）优先复用上一轮的政策对象。
            # 若继续按关键词重新检索，会因时间窗口、词序差异等原因漏检，
            # 用户明明刚看到政策，追问却说"没有可分析内容"。
            # 用户明确说"分析/解读/影响"时，计划里必须包含 analyze_policy。
            # Planner 有时把这些词判成 search，只检索不分析，用户看到
            # 标题和链接就以为"没有分析内容"。
            if task_type == "user_query" and self._wants_analysis(user_query):
                plan = self._ensure_analysis_step(plan)
                state.plan = plan.plan

            if task_type == "user_query" and self._is_elliptical_followup(user_query, context):
                plan = self._rewrite_followup_plan(plan, context, state)
                state.plan = plan.plan
                logger.info(
                    f"[{task_id}] 识别为省略式追问，复用上一轮政策对象: "
                    f"policy_id={getattr(state, 'followup_policy_id', None)}"
                )
            
            # Step 2: 执行计划
            total_steps = len(plan.plan)
            for step_index, step_plan in enumerate(plan.plan):
                if state.current_step >= self.max_steps:
                    logger.warning(f"[{task_id}] 达到最大步数限制")
                    break
                
                state.current_step += 1
                tool_name = step_plan.get("tool")
                reason = step_plan.get("reason", "")
                
                logger.info(f"[{task_id}] Step {state.current_step}: {tool_name} - {reason}")
                state.next_action = f"调用工具: {tool_name}"
                
                # 执行工具
                # 只有基础设施级失败（异常/未知工具）才会抛出 ToolExecutionError，
                # 工具返回的业务错误（如"正文过短"）属于正常计划分支，继续执行。
                try:
                    result = self._execute_tool(state, tool_name, plan.parameters)
                except ToolExecutionError as exc:
                    last_call = state.get_last_tool_call()
                    if last_call:
                        state.update_tool_result(last_call, None, error=str(exc))
                    return self._tool_failure_result(state, tool_name, str(exc))
                
                # 记录工具调用
                last_call = state.get_last_tool_call()
                if last_call:
                    state.update_tool_result(last_call, result)
                logger.info(
                    f"[{task_id}] Tool 结果: {tool_name} - "
                    f"{last_call.status.value if last_call else 'unknown'}"
                )
                
                # 根据工具结果更新状态
                self._process_tool_result(state, tool_name, result)
                
                # 自适应窗口回退：窄窗口结果不足时，用更宽窗口重试一次。
                # 只对定时任务生效（自然语言问答的 date_range 来自 planner，
                # 不应被强制回退）；已回退过的不重复回退。
                if tool_name == "search_policies" and self._should_fallback_search(state):
                    fallback = self._get_fallback_search_range(state)
                    if fallback:
                        logger.info(
                            f"[{task_id}] 检索结果不足，回退窗口重试: "
                            f"{self._task_search_date_range(state)} -> {fallback}"
                        )
                        state.search_date_range_override = fallback
                        try:
                            result = self._execute_tool(state, "search_policies", plan.parameters)
                        except ToolExecutionError as exc:
                            last_call = state.get_last_tool_call()
                            if last_call:
                                state.update_tool_result(last_call, None, error=str(exc))
                            return self._tool_failure_result(state, "search_policies", str(exc))
                        last_call = state.get_last_tool_call()
                        if last_call:
                            state.update_tool_result(last_call, result)
                        self._process_tool_result(state, tool_name, result)

                # 自然语言问答的窗口放宽：用户问"最近有什么政策"时，
                # planner 常给出"最近"（7 天窗口）。若该来源发布稀疏
                # （如湖北最新政策在 18 天前），窄窗口会返回 0 条，
                # 用户看到"未找到"，但实际数据是存在的。
                # 这里在窄窗口为空时逐级放宽：7天 -> 30天 -> 90天。
                if (
                    tool_name == "search_policies"
                    and not state.search_results
                    and self._scheduled_plan_steps(state.task_type) is None
                ):
                    for wider in ("最近30天", "最近90天"):
                        if state.search_date_range_override == wider:
                            continue
                        logger.info(
                            f"[{task_id}] 问答检索为空，放宽窗口重试: {wider}"
                        )
                        state.search_date_range_override = wider
                        try:
                            result = self._execute_tool(state, "search_policies", plan.parameters)
                        except ToolExecutionError:
                            break
                        last_call = state.get_last_tool_call()
                        if last_call:
                            state.update_tool_result(last_call, result)
                        self._process_tool_result(state, tool_name, result)
                        if state.search_results:
                            logger.info(
                                f"[{task_id}] 窗口放宽到 {wider} 后命中 "
                                f"{len(state.search_results)} 条"
                            )
                            break

                # 采集只是计划中的一步：只有当它是最后一步时才提前结束。
                # 否则会腰斩"采集 → 检索 → 分析 → 生成报告"这类复合计划
                #（日报/周报/月报全部依赖这条链路）。
                if tool_name == "collect_new_policies" and step_index == total_steps - 1:
                    state.next_action = "完成采集任务"
                    break

            # 定时报告的 generate_report 是整条链路的交付物。报告生成失败时
            # 不能把任务标记为完成，否则调度器会把"数据不足"之类的失败文案
            # 当作真实日报推送给用户。
            # 仅对定时任务生效：自然语言问答里报告只是可选分支，失败不应
            # 让整个对话轮次报错。
            planned_tools = [step.get("tool") for step in plan.plan]
            scheduled_report = scheduled_steps is not None
            if scheduled_report and "generate_report" in planned_tools and not state.report_path:
                report_error = state.report_error or "报告生成失败，未产生报告文件"
                logger.error(f"[{task_id}] 任务失败: {report_error}")
                state.status = TaskStatus.FAILED
                state.error = report_error
                state.final_result = f"处理失败: {report_error}"
                state.next_action = "报告生成失败"
                self._fail_agent_task(state, report_error)
                return AgentResult(
                    success=False,
                    answer=state.final_result,
                    raw_answer=self._extract_raw_answer(state),
                    state=state,
                    error=report_error,
                )

            # Step 3: 生成用户友好的最终回答（使用 Response Formatter）
            state.final_answer = self._format_final_response(state, user_query)
            state.final_result = state.final_answer
            state.next_action = "任务完成"
            
            state.status = TaskStatus.COMPLETED
            self._complete_agent_task(state)
            
            # 记录助手回答到记忆
            if use_legacy_memory:
                memory.add("assistant", state.final_answer, {"task_id": task_id})
            
            logger.info(f"[{task_id}] 处理完成")
            return AgentResult(
                success=True, 
                answer=state.final_answer, 
                raw_answer=self._extract_raw_answer(state),
                state=state
            )
            
        except Exception as e:
            logger.error(f"[{task_id}] 执行失败: {e}")
            state.status = TaskStatus.FAILED
            state.error = str(e)
            state.final_result = f"处理失败: {e}"
            error_msg = f"处理失败: {e}"
            state.next_action = "任务失败"
            self._fail_agent_task(state, str(e))
            if use_legacy_memory:
                memory.add("assistant", error_msg, {"task_id": task_id, "error": True})
            return AgentResult(
                success=False, 
                answer=error_msg, 
                state=state, 
                error=str(e)
            )
    
    def _execute_tool(self, state: AgentState, tool_name: str, params: Dict[str, Any]) -> Any:
        """执行工具"""
        tool = tool_registry.get(tool_name)
        if not tool:
            raise ToolExecutionError(tool_name, f"未知工具: {tool_name}")
        
        # 记录工具调用
        arguments = self._build_tool_arguments(tool_name, params, state)
        tool_call = state.add_tool_call(tool_name, arguments)
        tool_call.status = ToolCallStatus.EXECUTING

        # 新增：数据库执行记录
        record = self._create_execution_record(
            task_id=state.task_id,
            user_request=state.user_query,
            trigger_type=self.trigger_type,
            tool_name=tool_name,
            tool_args=self._serializable_args(tool_name, arguments)
        )
        
        # generate_report 需要读取当前 AgentState 才能汇总结果，
        # 无法只靠 plan.parameters 重建，因此在执行时注入 state。
        call_kwargs = dict(arguments)
        if tool_name == "generate_report":
            call_kwargs["state_or_results"] = state
        elif tool_name == "analyze_policies_batch":
            # 候选列表由受控状态注入，不进入工具参数审计记录。
            call_kwargs["policies"] = state.search_results
            # candidate_count 仅用于审计摘要，业务函数不接收该字段。
            call_kwargs.pop("candidate_count", None)

        try:
            start_time = datetime.now()
            result = tool.function(**call_kwargs)
            elapsed_ms = int((datetime.now() - start_time).total_seconds() * 1000)
            self._update_execution_record(record, status="success", result_summary=self._summarize_result(result), elapsed_ms=elapsed_ms)
            return result
        except Exception as e:
            logger.error(f"工具执行失败 {tool_name}: {e}")
            self._update_execution_record(record, status="failed", error=str(e))
            raise ToolExecutionError(tool_name, str(e)) from e

    def _tool_failure_result(
        self,
        state: AgentState,
        tool_name: str,
        error: Any,
    ) -> AgentResult:
        """把关键工具失败同步到内存状态、数据库和返回结果。"""
        state.status = TaskStatus.FAILED
        state.error = str(error)
        state.final_result = f"处理失败: {state.error}"
        state.next_action = f"工具失败: {tool_name}"
        self._fail_agent_task(state, state.error)
        return AgentResult(
            success=False,
            answer=state.final_result,
            raw_answer=self._extract_raw_answer(state),
            state=state,
            error=state.error,
        )

    @staticmethod
    def _serializable_args(tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """执行记录只保存可 JSON 序列化的参数摘要。"""
        if tool_name == "generate_report":
            return {
                "mode": arguments.get("mode"),
                "source_name": arguments.get("source_name"),
                "top_n": arguments.get("top_n"),
            }
        if tool_name == "analyze_policies_batch":
            # 批量分析的候选正文可能很长，执行记录只保留可审计摘要。
            return {
                "top_n": arguments.get("top_n"),
                "concurrency": arguments.get("concurrency"),
                "candidate_count": arguments.get("candidate_count"),
            }
        return arguments
    
    def _build_tool_arguments(self, tool_name: str, params: Dict[str, Any], state: AgentState) -> Dict[str, Any]:
        """构建工具参数"""
        if tool_name == "search_policies":
            # 定时报告的检索窗口以任务模板为准：planner 由 LLM 规划，
            # 同一句 prompt 每次给出的 date_range 都不一致（实测出现过
            # 空参数 / "今天" / "过去24小时"），而政府网站发布稀疏，
            # 窗口一旦收窄到 24 小时就会检索为空、日报失去内容。
            # 自然语言问答不受影响：它的 task_type 不在 TASK_TEMPLATES 中。
            task_range = self._task_search_date_range(state)
            date_range = task_range or params.get("date_range")

            # "最新/最近政策"这类语义不应设严格时间下界：地方政府发布稀疏，
            # 7/30 天窗口经常为空，用户会被告知"没有政策"，但实际是
            # 更早发布的政策才是该地区的最新一条。
            # 这种情况按发布时间倒序取最新 N 条即可。
            if not task_range and self._is_latest_query(state.user_query):
                date_range = None

            return {
                "region": self._normalize_search_filter(params.get("region"), "region"),
                "keyword": self._normalize_search_filter(params.get("keyword"), "keyword"),
                "date_range": date_range,
                "limit": 20
            }
        elif tool_name == "get_policy_detail":
            if state.search_results:
                first = state.search_results[0]
                if first.get("id"):
                    return {"policy_id": first["id"]}
                if first.get("url"):
                    return {"url": first["url"]}
            return {"policy_id": 0}
        elif tool_name == "search_historical_policies":
            return {
                "keyword": params.get("keyword", ""),
                "region": params.get("region"),
                "time_range": params.get("date_range"),
                "limit": 50
            }
        elif tool_name == "analyze_policy":
            # 按游标取「下一条」待分析政策：原先固定取 search_results[0]，
            # 导致报告里只有一条政策有分析结果，TOP10 退化成 TOP1。
            target = self._next_analyze_target(state)
            if target:
                if target.get("content") and len(target.get("content", "")) >= 50:
                    return {"policy_text": target.get("content", ""), "title": target.get("title", "")}
                # target 有正文以外的标识时，按它的 id/url 补抓正文。
                # 注意不能再往下走 policy_details 分支：那会把对象换成
                # 另一条政策，分析结果绑不到游标 target 上。
                from app.agent.tools import get_policy_detail
                if target.get("id"):
                    detail = get_policy_detail(policy_id=target["id"])
                elif target.get("url"):
                    detail = get_policy_detail(url=target["url"])
                else:
                    detail = None
                if detail and detail.get("content") and len(detail.get("content", "")) >= 50:
                    state.current_analyze_target = detail
                    return {
                        "policy_text": detail.get("content", ""),
                        "title": detail.get("title", "")
                    }
                logger.info(f"该政策正文不足，跳过分析: {str(target.get('title', ''))[:30]}")
                return {"policy_text": "", "title": ""}
            # 无检索结果时（自然语言问答可能先取详情），回退到详情对象
            if state.policy_details:
                policy = state.policy_details[0]
                return {
                    "policy_text": policy.get("content", ""),
                    "title": policy.get("title", "")
                }
            return {"policy_text": "", "title": ""}
        elif tool_name == "analyze_policies_batch":
            from app.config import get_report_top_n

            top_n = get_report_top_n()
            return {
                "top_n": top_n,
                "concurrency": settings.analyze_concurrency,
                "candidate_count": min(len(state.search_results), top_n),
            }
        elif tool_name == "analyze_policy_trend":
            return {
                "keyword": params.get("keyword") or (state.historical_data[0].get("keyword") if state.historical_data else ""),
                "region": params.get("region"),
                "time_range": params.get("date_range")
            }
        elif tool_name == "collect_new_policies":
            return {}
        elif tool_name == "generate_report":
            # state_or_results 由 _execute_tool 在调用时注入 AgentState
            from app.config import get_report_top_n

            return {
                "mode": (
                    params.get("mode")
                    or state.report_mode
                    or self._infer_report_mode(state)
                ),
                "source_name": params.get("source_name"),
                "top_n": get_report_top_n(),
            }
        return {}

    @staticmethod
    def _infer_report_mode(state: AgentState) -> str:
        """按任务类型推断报告周期，用户未显式指定 mode 时使用。"""
        mapping = {
            "daily_policy_scan": "daily",
            "weekly_policy_trend": "weekly",
            "monthly_policy_review": "monthly",
        }
        return mapping.get(state.task_type, "daily")

    def _scheduled_plan_steps(self, task_type: str) -> Optional[List[Dict[str, Any]]]:
        """定时任务返回确定性计划；自然语言问答返回 None 表示仍由 LLM 规划。

        延迟导入：scheduler 在模块顶层 import 了 policy_agent，
        这里再顶层 import 会形成循环依赖。
        """
        try:
            from app.agent.scheduler import TASK_TEMPLATES, build_task_plan
        except Exception as exc:
            logger.warning(f"读取定时任务计划失败，回退到 LLM 规划: {exc}")
            return None
        if task_type not in TASK_TEMPLATES:
            return None
        return build_task_plan(task_type, max_steps=self.max_steps)

    @staticmethod
    def _task_search_date_range(state: AgentState) -> Optional[str]:
        """取定时任务模板里配置的检索窗口。

        延迟导入：scheduler 在模块顶层 import 了 policy_agent，
        这里再顶层 import 会形成循环依赖。
        """
        try:
            from app.agent.scheduler import TASK_TEMPLATES
            # 回退覆盖优先：窄窗口结果不足时用更宽窗口重试
            if state.search_date_range_override:
                return state.search_date_range_override
            template = TASK_TEMPLATES.get(state.task_type) or {}
            return template.get("search_date_range")
        except Exception as exc:
            logger.warning(f"读取任务检索窗口失败，回退到 planner 参数: {exc}")
            return None

    def _should_fallback_search(self, state: AgentState) -> bool:
        """判断是否需要回退检索窗口。

        只对定时任务生效；已经回退过的不重复回退（避免无限循环）。
        自然语言问答的 date_range 来自 planner，不应被强制回退。
        """
        if state.search_date_range_override:
            return False  # 已回退过
        try:
            from app.agent.scheduler import TASK_TEMPLATES
            template = TASK_TEMPLATES.get(state.task_type) or {}
        except Exception:
            return False
        if not template:
            return False  # 自然语言问答不回退
        min_results = template.get("min_search_results")
        if not min_results or min_results <= 0:
            return False
        return len(state.search_results) < min_results

    def _get_fallback_search_range(self, state: AgentState) -> Optional[str]:
        """取回退检索窗口。fallback 与主窗口相同或不存在时返回 None。"""
        try:
            from app.agent.scheduler import TASK_TEMPLATES
            template = TASK_TEMPLATES.get(state.task_type) or {}
            fallback = template.get("search_date_range_fallback")
            current = template.get("search_date_range")
            if fallback and fallback != current:
                return fallback
            return None
        except Exception:
            return None

    @staticmethod
    def _normalize_search_filter(value: Optional[str], field_name: str) -> Optional[str]:
        """丢弃 planner 产出的多值枚举串。

        planner 经常把 prompt 里的枚举列表原样塞进 region/keyword，
        例如 region="国家、湖北、武汉、十堰"、keyword="民生、产业趋势、就业、人才"。
        而底层 build_region_filter 和 Policy.title.contains() 都按单值精确匹配，
        这种长串不可能命中任何记录，会让检索直接清零（实测 count=0）。
        实测拆开后每个单值都能命中（就业 6 条、人才 11 条），所以这里
        只在"拆开超过一个值"时置空，让检索退回按时间窗取全量。
        真正的单值（如 region="湖北"）原样保留。
        """
        if not value or not isinstance(value, str):
            return value

        value = value.strip()
        parts = [p for p in re.split(r"[,，、;；/|\s]+", value) if p]
        if len(parts) > 1:
            logger.info(f"planner 的 {field_name} 是多值枚举（{value}），已忽略以避免检索清零")
            return None

        # planner 有时会把整句用户提问塞进 region/keyword（如
        # region="最近湖北省有哪些政策"），这种句子不可能匹配任何字段，
        # 会让检索直接清零。这里判断"是否为已知的规范值"，
        # 不是则尝试从整句里提取，提取不到就置空回退全量检索。
        from app.data_layer.query_parser import query_parser as _qp
        _probe = _qp.parse(value)

        # 判定"是否已经是规范值"，关键看它是不是问句，而不是看长度。
        # 政策标题片段本身可能很长（如"优化调整部分行政审批事项"共12字），
        # 属于合法关键词，不能因为超过某个字数阈值就被置空。
        is_question = bool(re.search(r"[?？]|有哪些|有什么|是什么|怎么|如何|哪些", value))

        if field_name == "region":
            # 地区：只有能命中权威机构、且不是问句时才算规范值
            is_canonical = bool(_probe.authority) and not is_question
        else:
            # 关键词：不是问句即视为规范值
            is_canonical = not is_question

        if not is_canonical:
            parsed = _probe

            if field_name == "region":
                authority = (parsed.authority or "").strip()
                if authority:
                    logger.info(f"planner 的 region 是整句（{value}），提取为 {authority}")
                    return authority
            else:
                keywords = [k for k in (_qp.expand_synonyms(parsed.keywords) or []) if len(k) >= 2]
                if keywords:
                    picked = keywords[0]
                    logger.info(f"planner 的 keyword 是整句（{value}），提取为 {picked}")
                    return picked

            logger.info(f"planner 的 {field_name} 是整句（{value}），已忽略以避免检索清零")
            return None

        return value
    
    def _process_tool_result(self, state: AgentState, tool_name: str, result: Any):
        """处理工具结果，更新状态"""
        if tool_name == "search_policies":
            if isinstance(result, list):
                state.search_results = result
                state.policies = result
                logger.info(f"搜索到 {len(result)} 条政策")
        elif tool_name == "get_policy_detail":
            if result:
                state.policy_details.append(result)
        elif tool_name == "search_historical_policies":
            if isinstance(result, list):
                state.historical_data = result
                state.policies = result
                logger.info(f"历史搜索到 {len(result)} 条政策")
        elif tool_name == "analyze_policy":
            if not isinstance(result, dict):
                state.analysis_failures.append({
                    "error": "分析工具返回了非字典结果",
                    "result_type": type(result).__name__,
                })
                logger.warning("分析结果类型无效: %s", type(result).__name__)
                return

            # 先绑定实际分析对象，失败结果也保留可追溯的政策标识。
            self._bind_analysis_to_source_policy(state, result)
            if result.get("error"):
                state.analysis_failures.append({
                    "error": str(result["error"]),
                    "policy_id": result.get("policy_id"),
                    "url": result.get("url"),
                    "title": result.get("title"),
                })
                logger.warning("分析失败，未进入报告: %s", result["error"])
                return

            # 兜底结果标记为 failed 时属于不可用分析。
            if result.get("_analysis_status") == ANALYSIS_STATUS_FAILED:
                state.analysis_failures.append({
                    "error": result.get("_analysis_error") or "分析失败",
                    "policy_id": result.get("policy_id"),
                    "url": result.get("url"),
                    "title": result.get("title"),
                    "_analysis_source": result.get("_analysis_source"),
                })
                logger.warning(
                    "分析状态为失败，未进入报告: %s",
                    str(result.get("title", ""))[:30],
                )
                return

            if not is_trusted_analysis(result):
                status = result.get("_analysis_status", "unknown")
                state.degraded_analyses.append(result)
                logger.warning(
                    "分析仅为降级结果（%s），未落库、未进入正式报告: %s",
                    status, str(result.get("title", ""))[:30],
                )
                return

            # 落库放在绑定之后：_bind_analysis_to_source_policy 刚把权威
            # policy_id 写进 result，这里才拿得到该关联到哪条政策。
            # save_analysis 会把 _analysis_* 元数据从持久化数据中剥离。
            PolicyService.save_analysis(
                result.get("policy_id"), result, url=result.get("url")
            )
            state.analysis_results.append(result)
            state.analyses.append(result)
            logger.info(f"分析完成: 趋势评分 {result.get('趋势评分', 0)}")
        elif tool_name == "analyze_policies_batch":
            if not isinstance(result, dict):
                state.analysis_failures.append({
                    "error": "批量分析工具返回了非字典结果",
                    "result_type": type(result).__name__,
                })
                logger.warning(
                    "批量分析结果类型无效: %s", type(result).__name__
                )
                return

            container_error = result.get("error")
            if container_error:
                state.analysis_failures.append({
                    "error": str(container_error),
                    "requested": result.get("requested"),
                })
                logger.warning("批量分析失败: %s", container_error)
                return

            failed_count = 0
            for failure in result.get("failures") or []:
                if not isinstance(failure, dict):
                    failure = {"error": str(failure)}
                state.analysis_failures.append({
                    "policy_id": failure.get("policy_id"),
                    "url": failure.get("url"),
                    "title": failure.get("title"),
                    "error": failure.get("error") or "批量分析失败",
                    "_analysis_source": failure.get("_analysis_source"),
                })
                failed_count += 1

            trusted_count = 0
            degraded_count = 0
            for analysis in result.get("results") or []:
                if not isinstance(analysis, dict):
                    state.analysis_failures.append({
                        "error": "批量分析返回了非字典条目",
                        "result_type": type(analysis).__name__,
                    })
                    continue

                policy_id = analysis.get("policy_id")
                url = analysis.get("url")
                title = analysis.get("title")
                if analysis.get("_analysis_status") == ANALYSIS_STATUS_FAILED:
                    state.analysis_failures.append({
                        "policy_id": policy_id,
                        "url": url,
                        "title": title,
                        "error": analysis.get("_analysis_error") or "分析失败",
                        "_analysis_source": analysis.get("_analysis_source"),
                    })
                    failed_count += 1
                    continue
                if not is_trusted_analysis(analysis):
                    state.degraded_analyses.append(analysis)
                    degraded_count += 1
                    logger.warning(
                        "批量分析中的降级结果未落库、未进入正式报告: %s",
                        str(title or "")[:30],
                    )
                    continue

                PolicyService.save_analysis(
                    policy_id, analysis, url=url
                )
                state.analysis_results.append(analysis)
                state.analyses.append(analysis)
                trusted_count += 1

            logger.info(
                "批量分析完成: 可信 %s 条，降级 %s 条，失败 %s 条",
                trusted_count, degraded_count, failed_count,
            )
        elif tool_name == "generate_report":
            if isinstance(result, dict):
                if result.get("error"):
                    state.report_error = str(result["error"])
                    logger.warning(f"报告生成失败: {result['error']}")
                elif result.get("report_path"):
                    state.report_path = result["report_path"]
                    state.report_error = None
                    state.pushed_policies = result.get("pushed_policies") or []
                    logger.info(f"报告已生成: {state.report_path}")

    @staticmethod
    def _next_analyze_target(state: AgentState) -> Optional[Dict[str, Any]]:
        """取下一条待分析政策，并推进游标。

        原实现固定取 search_results[0]，无论调用多少次 analyze_policy，
        分析的都是同一条政策，报告里只有一条有分析结果，TOP10 退化成 TOP1。

        游标存在 AgentState.analyze_cursor 上，不用 len(analysis_results) 代替：
        正文过短导致分析失败时结果不入列，用长度当游标会一直卡在同一条空转。
        游标走到头返回 None，调用方自然跳过，不再浪费工具调用。
        """
        if not state.search_results:
            return None
        if state.analyze_cursor >= len(state.search_results):
            logger.info(f"待分析政策已全部处理（{len(state.search_results)} 条）")
            return None
        target = state.search_results[state.analyze_cursor]
        state.analyze_cursor += 1
        if isinstance(target, dict):
            state.current_analyze_target = target
        return target if isinstance(target, dict) else None

    @staticmethod
    def _bind_analysis_to_source_policy(state: AgentState, analysis: Dict[str, Any]) -> None:
        """把分析结果绑回它实际分析的那条政策。

        analyze_policy 的返回体是 LLM 的原始 JSON：既没有 url，
        title 也可能被模型改写或截断。报告侧靠 title/url 等值匹配回政策，
        两边对不上就会把这份分析丢掉，最终报告退化成「今日无重要政策更新」。
        这里在结果入库 state 之前，用权威的政策元数据覆盖回去：
        分析对象由 _build_tool_arguments 经游标选出（current_analyze_target），
        绑定关系在执行器这里是最准确的。

        搜索结果与详情的键名是 id（见 PolicyService._policy_item_to_dict），
        这里统一落成 policy_id，供 ReportService 做稳定匹配。
        """
        source: Optional[Dict[str, Any]] = state.current_analyze_target
        if not isinstance(source, dict):
            source = None
        # 回退链保持 A 阶段行为：游标没有记录（例如分析结果不是经
        # _build_tool_arguments 产生的）时，仍按第一条检索结果绑，
        # 再退到详情对象。只认 current_analyze_target 会让这类调用绑不上。
        if not source and state.search_results:
            source = state.search_results[0]
        if not source and state.policy_details:
            source = state.policy_details[0]
        if not isinstance(source, dict):
            return
        policy_id = source.get("policy_id") or source.get("id")
        if policy_id:
            analysis["policy_id"] = policy_id
        if source.get("url"):
            analysis["url"] = source["url"]
        if source.get("title"):
            # 权威标题来自政策条目，覆盖 LLM 可能改写过的 title
            analysis["title"] = source["title"]

    def _create_execution_record(self, task_id: str, user_request: str,
                                 trigger_type: str, tool_name: str, tool_args: Dict[str, Any]) -> Optional[AgentExecutionRecord]:
        """创建执行记录"""
        db = SessionLocal()
        try:
            record = AgentExecutionRecord(
                task_id=task_id,
                user_request=user_request,
                trigger_type=trigger_type,
                tool_name=tool_name,
                tool_args=json.dumps(tool_args, ensure_ascii=False) if tool_args else None,
                status="running"
            )
            db.add(record)
            db.commit()
            db.refresh(record)
            return record
        except Exception as e:
            logger.error(f"创建执行记录失败: {e}")
            db.rollback()
            return None
        finally:
            db.close()

    def _update_execution_record(self, record: Optional[AgentExecutionRecord], status: str,
                                 result_summary: str = None, elapsed_ms: int = None, error: str = None):
        """更新执行记录"""
        if not record:
            return
        db = SessionLocal()
        try:
            db_record = db.query(AgentExecutionRecord).filter(AgentExecutionRecord.id == record.id).first()
            if db_record:
                db_record.status = status
                db_record.tool_result_summary = result_summary
                db_record.execution_time = elapsed_ms
                db_record.error_message = error
                db.commit()
        except Exception as e:
            logger.error(f"更新执行记录失败: {e}")
            db.rollback()
        finally:
            db.close()

    def _ensure_agent_task(self, state: AgentState) -> None:
        """统一创建/更新用户任务和定时任务的执行记录"""
        db = SessionLocal()
        try:
            task = db.query(AgentTask).filter(AgentTask.task_id == state.task_id).first()
            if not task:
                task = AgentTask(
                    task_id=state.task_id,
                    trigger_type=state.trigger_type,
                    task_type=state.task_type,
                    user_request=state.user_request or state.user_query,
                    status="running",
                    started_at=datetime.now(),
                )
                db.add(task)
            else:
                task.trigger_type = state.trigger_type
                task.task_type = state.task_type
                task.user_request = state.user_request or state.user_query
                task.status = "running"
                task.started_at = task.started_at or datetime.now()
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.error(f"创建统一 Agent 任务失败 task_id={state.task_id}: {exc}")
        finally:
            db.close()

    def _complete_agent_task(self, state: AgentState) -> None:
        """持久化 Agent 结果；定时报告由 Scheduler 负责最终投递终态。

        定时任务采用两段式提交：Agent 生成报告后只写入可恢复的结果上下文，
        状态保持 ``running``；只有 Scheduler 确认通知结果后才写
        ``completed``/``failed``。否则在“报告已生成、通知尚未执行”的窗口
        崩溃，下一轮会把任务误判为已完成后永久跳过。
        """
        db = SessionLocal()
        try:
            task = db.query(AgentTask).filter(AgentTask.task_id == state.task_id).first()
            if task:
                if state.task_type in {
                    "daily_policy_scan",
                    "weekly_policy_trend",
                    "monthly_policy_review",
                }:
                    task.status = "running"
                    task.result = json.dumps(
                        {
                            "kind": "scheduled_report",
                            "summary": state.final_result or "",
                            "report_path": state.report_path,
                            "pushed_policies": state.pushed_policies or [],
                            "delivery_status": "pending",
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    task.error = None
                    task.completed_at = None
                else:
                    task.status = "completed"
                    task.result = (state.final_result or "")[:200]
                    task.error = None
                    task.completed_at = datetime.now()
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.error(f"更新统一 Agent 任务完成状态失败 task_id={state.task_id}: {exc}")
        finally:
            db.close()

    def _fail_agent_task(self, state: AgentState, error_message: str) -> None:
        """标记 Agent 失败；定时报告保留已完成产物供投递层恢复。"""
        db = SessionLocal()
        try:
            task = db.query(AgentTask).filter(AgentTask.task_id == state.task_id).first()
            if task:
                task.status = "failed"
                task.error = error_message[:1000]
                if state.task_type in {
                    "daily_policy_scan",
                    "weekly_policy_trend",
                    "monthly_policy_review",
                } and state.report_path:
                    task.result = json.dumps(
                        {
                            "kind": "scheduled_report",
                            "summary": error_message,
                            "report_path": state.report_path,
                            "pushed_policies": state.pushed_policies or [],
                            "delivery_status": "pending",
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                task.completed_at = datetime.now()
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.error(f"更新统一 Agent 任务失败状态 task_id={state.task_id}: {exc}")
        finally:
            db.close()

    def _summarize_result(self, result: Any) -> str:
        """提取工具结果摘要"""
        if isinstance(result, list):
            return f"返回 {len(result)} 条结果"
        if isinstance(result, dict):
            if "error" in result:
                return f"错误: {result['error']}"
            if "success" in result:
                return f"成功: {json.dumps(result, ensure_ascii=False)[:200]}"
            return json.dumps(result, ensure_ascii=False)[:200]
        return str(result)[:200]
    
    def _format_final_response(self, state: AgentState, user_query: str) -> str:
        """使用 Response Formatter 生成用户友好回答"""
        # 构建 agent_result 供 formatter 使用
        agent_result = {
            "search_results": state.search_results,
            "analysis_results": state.analysis_results,
            "policy_details": state.policy_details,
            "historical_data": state.historical_data,
            "tool_calls": [
                {
                    "name": tc.name,
                    "arguments": tc.arguments,
                    "status": tc.status.value,
                    "error": tc.error
                }
                for tc in state.tool_calls
            ],
            "report_path": state.report_path,
            "report_error": state.report_error,
            "degraded_analyses": state.degraded_analyses,
            "analysis_failures": state.analysis_failures,
            # 检索条件：空结果时格式化层需要据此生成可定位的原因说明，
            # 而不是只回一句"未找到"。
            "search_filters": self._describe_search_filters(state),
            "answer": ""  # 将由 formatter 生成
        }
        
        # 使用 Response Formatter
        formatted = format_agent_response(agent_result, user_query)
        return formatted
    
    #: 明确要求分析的措辞
    ANALYSIS_MARKERS = ("分析", "解读", "影响", "详细说说", "展开讲讲", "深入")

    @classmethod
    def _wants_analysis(cls, query: str) -> bool:
        return any(m in (query or "") for m in cls.ANALYSIS_MARKERS)

    @staticmethod
    def _ensure_analysis_step(plan: Any) -> Any:
        """确保计划末尾包含 analyze_policy。"""
        from app.agent.planner import Plan

        tools = [s.get("tool") for s in plan.plan]
        if "analyze_policy" in tools:
            return plan
        new_plan = list(plan.plan)
        if "get_policy_detail" not in tools and "search_policies" in tools:
            new_plan.append({"step": len(new_plan) + 1, "tool": "get_policy_detail",
                             "reason": "获取政策全文"})
        new_plan.append({"step": len(new_plan) + 1, "tool": "analyze_policy",
                         "reason": "对政策全文进行深度分析"})
        return Plan(
            intent="analyze",
            parameters=plan.parameters,
            plan=new_plan,
            needs_detail=True,
            needs_trend=plan.needs_trend,
            raw_response=plan.raw_response,
        )

    #: "最新政策"语义标记：按时间倒序取最新，不设严格时间下界
    LATEST_INTENT_MARKERS = (
        "最新政策", "最新一条", "最新发布", "最新的政策",
        "最新政策内容", "最新的政策内容",
    )

    @classmethod
    def _is_latest_query(cls, query: str) -> bool:
        """判断是否为"最新政策"类查询（应按时间倒序取最新，而非限定窗口）。"""
        text = (query or "").strip()
        return any(marker in text for marker in cls.LATEST_INTENT_MARKERS)

    #: 省略式追问词：无主语，指代上一轮对象
    ELLIPTICAL_FOLLOWUPS = {
        "分析一下", "分析下", "详细说说", "展开讲讲", "展开说说",
        "具体说说", "深入点", "深入分析", "继续说", "再说说",
        "解释一下", "解释下", "详细点", "具体点", "继续",
    }

    #: 追问中可用于解析"上一轮主题"的地区词
    _FOLLOWUP_REGIONS = (
        "国务院", "国家", "湖北省", "湖北", "武汉市", "武汉", "十堰市", "十堰",
    )

    @classmethod
    def _is_elliptical_followup(cls, query: str,
                                context: Optional[List[Dict[str, str]]]) -> bool:
        """判断是否为省略式追问（不含明确主题，依赖上一轮）。"""
        if not context:
            return False
        text = (query or "").strip().rstrip("。！？!?.~ ")
        if text not in cls.ELLIPTICAL_FOLLOWUPS:
            return False
        # 追问本身不应含地区/主题，否则是独立问题
        return not any(r in text for r in cls._FOLLOWUP_REGIONS)

    @classmethod
    def _extract_prior_region(cls, context: List[Dict[str, str]]) -> Optional[str]:
        """从最近的用户消息中提取地区。"""
        for item in reversed(context or []):
            if item.get("role") != "user":
                continue
            content = item.get("content") or ""
            for region in cls._FOLLOWUP_REGIONS:
                if region in content:
                    return region
        return None

    def _rewrite_followup_plan(self, plan: Any, context: List[Dict[str, str]],
                               state: AgentState) -> Any:
        """把省略式追问改写为"分析上一轮主题"的确定性计划。

        优先用 get_policy_detail 按地区+最新取详情，再 analyze_policy，
        完全绕开关键词重检索，避免词序/窗口导致的漏检。
        """
        from app.agent.planner import Plan

        region = self._extract_prior_region(context)
        state.region = region or state.region
        state.followup_policy_id = getattr(state, "followup_policy_id", None)

        return Plan(
            intent="analyze",
            parameters={
                **(plan.parameters or {}),
                "region": state.region,
                # 追问不指定关键词，避免用上一轮标题片段误匹配
                "keyword": "",
                "date_range": None,
                "goal": "分析上一轮提到的政策",
            },
            plan=[
                {"step": 1, "tool": "search_policies",
                 "reason": "定位上一轮地区的政策（不设关键词）"},
                {"step": 2, "tool": "analyze_policy",
                 "reason": "分析首条政策"},
            ],
            needs_detail=True,
            raw_response="followup-rewrite",
        )

    @staticmethod
    def _describe_search_filters(state: AgentState) -> Dict[str, Any]:
        """把本次检索条件整理成可读结构，供空结果原因说明使用。

        返回 region/keyword/date_range 的原始值，并额外给出人类可读的
        地域描述与时间窗口描述，避免格式化层再次解析。
        """
        region = (state.region or "").strip() or None
        keyword = (state.keyword or "").strip() or None
        date_range = (
            state.search_date_range_override
            or state.date_range
            or ""
        ).strip() or None

        # 从已执行的工具调用中取实际使用的参数（优先于 planner 原始值）
        for call in reversed(state.tool_calls):
            if call.name == "search_policies" and isinstance(call.arguments, dict):
                region = (call.arguments.get("region") or region or None)
                keyword = (call.arguments.get("keyword") or keyword or None)
                date_range = (call.arguments.get("date_range") or date_range or None)
                break

        return {
            "region": region,
            "keyword": keyword,
            "date_range": date_range,
        }

    def _extract_raw_answer(self, state: AgentState) -> str:
        """提取原始工具输出（用于调试）"""
        parts = []
        
        if state.search_results:
            parts.append(f"=== 搜索结果 ({len(state.search_results)} 条) ===")
            for i, r in enumerate(state.search_results[:5], 1):
                parts.append(f"  {i}. {r.get('title', 'N/A')} | {r.get('source_name', 'N/A')} | Score: {r.get('final_score', r.get('importance_score', 0))}")
        
        if state.analysis_results:
            parts.append(f"=== 分析结果 ({len(state.analysis_results)} 条) ===")
            for a in state.analysis_results:
                parts.append(f"  总结: {a.get('一句话总结', 'N/A')}")
                parts.append(f"  评分: {a.get('趋势评分', 0)}")
        
        if state.tool_calls:
            parts.append("=== 执行步骤 ===")
            for tc in state.tool_calls:
                status = "✓" if tc.status.value == "success" else "✗"
                parts.append(f"  {status} {tc.name}({tc.arguments})")
        
        return "\n".join(parts)


# 全局 Agent 实例
policy_agent = PolicyAgent()

print("PolicyAgent initialized")
