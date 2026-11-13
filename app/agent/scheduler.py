"""定时任务调度器
只负责触发 Agent Task；不直接执行采集、分析或报告生成。
"""

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from app.database import SessionLocal, init_db
from app.logger import logger
from app.models.agent_task import AgentTask
from app.agent.policy_agent import policy_agent
from app.config import settings
from app.notifiers.safety import redact_sensitive_text, safe_exception_message
from app.notifiers.service import NotificationService
from app.services.source_diagnostic import run_source_diagnostics


TASK_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "daily_policy_scan": {
        "prompt": "检查过去24小时国家、湖北、武汉、十堰发布的重要政策，重点关注民生、产业趋势、就业和人才政策，生成政策日报。",
        "cron": {"hour": 8, "minute": 0},
        # 检索窗口：定时报告以此为准，不由 planner 的 LLM 规划决定。
        # 自适应窗口：先用窄窗口（3 天），结果少于 min_search_results 时
        # 回退到 fallback 窗口（7 天）。政府网站发布稀疏，实测 3 天窗口
        # 经常为 0 条，但数据新鲜时（3 天内有政策）日报能真正"日报化"；
        # 数据不新鲜时回退到 7 天，保证报告不会空。
        "search_date_range": "最近3天",
        "search_date_range_fallback": "最近7天",
        "min_search_results": 2,
        "report_mode": "daily",
    },
    "weekly_policy_trend": {
        "prompt": "分析过去7天重点政策变化，并结合历史政策识别产业和民生趋势，生成周度趋势报告。",
        "cron": {"day_of_week": "mon", "hour": 8, "minute": 0},
        "search_date_range": "最近7天",
        "search_date_range_fallback": "最近14天",
        "min_search_results": 2,
        "report_mode": "weekly",
    },
    "monthly_policy_review": {
        "prompt": "分析过去30天重点政策及趋势变化，形成月度政策报告。",
        "cron": {"day": 1, "hour": 8, "minute": 0},
        "search_date_range": "最近30天",
        "search_date_range_fallback": "最近60天",
        "min_search_results": 2,
        "report_mode": "monthly",
    },
}


def build_task_plan(task_type: str, max_steps: int = 10) -> List[Dict[str, Any]]:
    """为定时任务构造固定的四步确定性计划。

    采集 → 检索 → 批量分析 → 生成报告。TOP N 由批量分析工具内部处理，
    不再展开成 N 个 Agent 工具步，因此结果数量不会随 max_steps 被截断。

    自然语言问答（task_type=user_query）不走这里，仍由 planner 规划。
    """
    if task_type not in TASK_TEMPLATES:
        return []
    if max_steps < 4:
        logger.error(
            "任务 %s 需要至少 4 个执行步骤，当前 max_steps=%s，无法生成计划",
            task_type, max_steps,
        )
        return []

    return [
        {"step": 1, "tool": "collect_new_policies", "reason": "采集最新政策并落库"},
        {"step": 2, "tool": "search_policies", "reason": "按任务配置的检索窗口取政策"},
        {"step": 3, "tool": "analyze_policies_batch", "reason": "一次批量分析排序后的 TOP N 条政策"},
        {"step": 4, "tool": "generate_report", "reason": "汇总可信分析结果生成报告"},
    ]


def _update_task(task_id: str, **fields) -> None:
    """更新任务记录中的指定字段"""
    db = SessionLocal()
    try:
        task = db.query(AgentTask).filter(AgentTask.task_id == task_id).first()
        if task:
            for key, value in fields.items():
                setattr(task, key, value)
            db.commit()
    except Exception as exc:
        logger.error(f"更新 Agent 任务失败 task_id={task_id}: {exc}")
        db.rollback()
    finally:
        db.close()


def build_execution_id(task_type: str, when: Optional[datetime] = None) -> str:
    """为定时任务生成确定性的执行标识：周期 + 任务类型。

    同一个周期内重复触发（Scheduler misfire、进程重启后补跑、容器重启）
    必须命中同一个 task_id，才能让 run_agent_task 查到已完成的记录并跳过，
    避免同一天重复推送日报。用户显式传入 request_id 时不走这里，保留人工重跑能力。

    - daily:   2026-09-17 -> daily_policy_scan:2026-09-17
    - weekly:  2026-W38   -> weekly_policy_trend:2026-W38
    - monthly: 2026-09    -> monthly_policy_review:2026-09
    """
    moment = when or datetime.now()
    if task_type == "daily_policy_scan":
        period = moment.strftime("%Y-%m-%d")
    elif task_type == "weekly_policy_trend":
        iso_year, iso_week, _ = moment.isocalendar()
        period = f"{iso_year}-W{iso_week:02d}"
    elif task_type == "monthly_policy_review":
        period = moment.strftime("%Y-%m")
    else:
        return f"{task_type}:{moment.strftime('%Y-%m-%d')}"
    return f"{task_type}:{period}"


def _encode_task_result(
    *,
    summary: str,
    report_path: Optional[str],
    pushed_policies: Optional[List[Dict[str, Any]]],
    delivery_status: Optional[str] = None,
    notification_results: Optional[Dict[str, Dict[str, Any]]] = None,
) -> str:
    """把定时报告结果编码为兼容 JSON，供任务查询和失败后重试投递。"""
    payload = {
        "kind": "scheduled_report",
        "summary": summary,
        "report_path": report_path,
        "pushed_policies": pushed_policies or [],
    }
    if delivery_status:
        payload["delivery_status"] = delivery_status
    if notification_results is not None:
        # 只保留可诊断字段，原始错误已由通知服务脱敏。
        payload["notification_results"] = {
            channel: {
                key: value
                for key, value in result.items()
                if key in {"status", "reason", "error", "chunks", "sent_chunks"}
            }
            for channel, result in notification_results.items()
        }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _decode_task_result(value: Optional[str]) -> Dict[str, Any]:
    """解析新格式结果，并兼容历史 ``报告路径: ...`` 文本。"""
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        payload = None
    if isinstance(payload, dict) and payload.get("kind") == "scheduled_report":
        return payload
    if text.startswith("报告路径:"):
        return {
            "kind": "scheduled_report",
            "summary": text,
            "report_path": text.split(":", 1)[1].strip() or None,
            "pushed_policies": [],
        }
    return {}


def _notification_outcome(
    results: Dict[str, Dict[str, Any]],
    task_id: str,
) -> Dict[str, Any]:
    """把渠道返回值归并为可决策的投递终态。

    ``skipped`` 不能一概视为成功或失败：已发送、未配置、并发占用和
    空内容语义不同。对结果不确定的渠道，再查询通知台账消歧。
    """
    delivered: List[str] = []
    failed: List[str] = []
    not_delivered: List[str] = []
    uncertain: List[str] = []

    for channel, item in results.items():
        status = item.get("status")
        reason = str(item.get("reason") or "")
        if status == "success":
            delivered.append(channel)
        elif status == "failed":
            failed.append(channel)
        elif status == "skipped" and "通知已发送" in reason:
            delivered.append(channel)
        elif status == "skipped" and ("未配置" in reason or "内容为空" in reason):
            not_delivered.append(channel)
        else:
            uncertain.append(channel)

    if uncertain:
        try:
            ledger = NotificationService.get_status(task_id)
            ledger_items = {
                item.get("channel"): item for item in ledger.get("records", [])
            }
        except Exception as exc:
            logger.warning(
                "通知结果不确定且查询台账失败 task_id=%s: %s",
                task_id,
                redact_sensitive_text(exc),
            )
            ledger_items = {}

        unresolved: List[str] = []
        for channel in uncertain:
            record = ledger_items.get(channel) or {}
            record_status = record.get("status")
            if record_status == "success":
                delivered.append(channel)
            elif record_status in {"failed", "stale"}:
                failed.append(channel)
            else:
                unresolved.append(channel)
        uncertain = unresolved

    if delivered:
        outcome = "partial" if (failed or uncertain) else "success"
    elif failed:
        outcome = "failed"
    elif uncertain:
        outcome = "uncertain"
    else:
        outcome = "not_configured"

    return {
        "status": outcome,
        "delivered_channels": delivered,
        "failed_channels": failed,
        "not_delivered_channels": not_delivered,
        "uncertain_channels": uncertain,
    }


def _read_report_content(
    report_path: Optional[str],
    task_id: str,
) -> Optional[str]:
    """读取最终报告正文；报告路径缺失或不可用时返回 None。

    通知层只允许投递完整报告文件，不得退回 Agent 摘要。摘要无法替代
    报告正文，误发会让用户把任务完成提示当成政策报告。
    """
    if not report_path:
        logger.error("定时任务未生成报告文件 task_id=%s", task_id)
        return None

    report_file = Path(report_path)
    try:
        if not report_file.is_file():
            logger.error("定时报告文件不存在 task_id=%s path=%s", task_id, report_path)
            return None
        content = report_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        logger.error(
            "读取定时报告失败 task_id=%s: %s",
            task_id,
            safe_exception_message(exc),
        )
        return None

    if not content.strip():
        logger.error("定时报告内容为空 task_id=%s path=%s", task_id, report_path)
        return None
    return content


def _deliver_scheduled_report(
    *,
    task_id: str,
    task_type: str,
    trigger_type: str,
    report_path: Optional[str],
    pushed_policies: Optional[List[Dict[str, Any]]],
    previous_result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """投递最终报告并持久化可恢复的终态。

    报告成功生成但通知失败时，任务必须是 ``failed``，同时保留
    ``report_path``。同周期再次触发可据此只重试通知，不重新执行 Agent。
    """
    previous_result = previous_result or {}
    reports = pushed_policies or previous_result.get("pushed_policies") or []
    content = _read_report_content(report_path, task_id)
    if content is None:
        error = "报告文件缺失、不可读或为空，无法完成通知"
        # 不保留失效路径，否则下一周期会反复重试同一个不存在的文件，
        # 永远无法进入 Agent 重新生成报告的分支。
        _update_task(
            task_id,
            status="failed",
            result=_encode_task_result(
                summary="报告产物不可用，等待重新生成",
                report_path=None,
                pushed_policies=reports,
                delivery_status="failed",
            ),
            error=error,
            completed_at=datetime.now(),
        )
        return {
            "task_id": task_id,
            "status": "failed",
            "delivery_status": "failed",
            "report_path": None,
            "error": error,
        }

    channels = [item.strip() for item in settings.notify_channels.split(",") if item.strip()]
    metadata = {
        "task_id": task_id,
        "task_type": task_type,
        "trigger_type": trigger_type,
    }
    notification_results: Dict[str, Dict[str, Any]] = {}
    if not channels:
        outcome = {
            "status": "not_configured",
            "delivered_channels": [],
            "failed_channels": [],
            "not_delivered_channels": [],
            "uncertain_channels": [],
        }
    else:
        try:
            notification_results = NotificationService.notify_channels(
                channels=channels,
                content=content,
                metadata=metadata,
            )
            missing_channels = [
                channel for channel in channels if channel not in notification_results
            ]
            if missing_channels:
                # 渠道已配置但通知服务没有返回该渠道结果，属于契约异常。
                # 不能当成“未配置成功”，否则会错误登记为已完成并停止投递。
                for channel in missing_channels:
                    notification_results[channel] = {
                        "status": "failed",
                        "error": "通知服务未返回该渠道的投递结果",
                    }
                logger.error(
                    "通知服务返回结果缺少渠道 task_id=%s channels=%s",
                    task_id,
                    ",".join(missing_channels),
                )
            outcome = _notification_outcome(notification_results, task_id)
        except Exception as exc:
            safe_error = safe_exception_message(exc)
            logger.error("定时报告通知执行异常 task_id=%s: %s", task_id, safe_error)
            notification_results = {
                channel: {"status": "failed", "error": safe_error}
                for channel in channels
            }
            outcome = {
                "status": "failed",
                "delivered_channels": [],
                "failed_channels": channels,
                "not_delivered_channels": [],
                "uncertain_channels": [],
            }

    delivery_status = outcome["status"]
    delivered = bool(outcome["delivered_channels"])
    if delivered:
        # 只有至少一个渠道实际送达才登记去重；否则下次必须仍可推送。
        try:
            from app.services.push_dedup import push_dedup_service
            push_dedup_service.mark_pushed(reports, task_id=task_id)
        except Exception as exc:
            logger.error(
                "登记推送去重台账失败 task_id=%s: %s",
                task_id,
                safe_exception_message(exc),
            )

    if delivery_status == "success":
        status = "completed"
        error = None
        summary = "定时报告已成功投递"
    elif delivery_status == "partial":
        status = "completed"
        failed = sorted(set(outcome["failed_channels"] + outcome["uncertain_channels"]))
        error = f"报告已生成并部分送达，以下渠道失败或不确定: {', '.join(failed)}"
        summary = "定时报告已部分投递，请检查失败渠道"
    elif delivery_status == "not_configured":
        status = "completed"
        error = "报告已生成，但未配置任何通知渠道，报告未实际投递"
        summary = "定时报告已生成，未实际投递"
    else:
        status = "failed"
        failed = sorted(
            set(outcome["failed_channels"] + outcome["uncertain_channels"])
        )
        error = (
            "报告已生成，但通知全部失败或投递结果不确定，"
            f"以下渠道未送达: {', '.join(failed)}"
        )
        summary = "定时报告生成成功，通知失败，等待重试"
        logger.error("定时报告通知未送达 task_id=%s status=%s", task_id, delivery_status)

    encoded = _encode_task_result(
        summary=summary,
        report_path=report_path,
        pushed_policies=reports,
        delivery_status=delivery_status,
        notification_results=notification_results,
    )
    _update_task(
        task_id,
        status=status,
        result=encoded,
        error=error,
        completed_at=datetime.now(),
    )
    logger.info(
        "定时报告投递终态 task_id=%s delivery_status=%s delivered=%s",
        task_id,
        delivery_status,
        ",".join(outcome["delivered_channels"]) or "-",
    )
    return {
        "task_id": task_id,
        "status": status,
        "delivery_status": delivery_status,
        "report_path": report_path,
        "delivered_channels": outcome["delivered_channels"],
        "failed_channels": outcome["failed_channels"],
        "uncertain_channels": outcome["uncertain_channels"],
        "error": error,
    }


def run_agent_task(
    task_type: str,
    trigger_type: str = "scheduled",
    request_id: Optional[str] = None,
    timeout_seconds: int = 600,
    max_retries: int = 2,
    agent_runner: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """创建并执行一个 Agent Task，并完成报告投递闭环。

    - 已成功投递的阶段任务直接跳过；
    - 报告已生成但通知失败时，仅重试通知，不重新执行 Agent；
    - ``max_retries`` 只约束 Agent 超时/执行异常，不消耗于通知重试。
    """
    init_db()
    if task_type not in TASK_TEMPLATES:
        raise ValueError(f"未知任务类型: {task_type}")

    task_id = request_id or build_execution_id(task_type)
    prompt = TASK_TEMPLATES[task_type]["prompt"]
    previous_result: Dict[str, Any] = {}

    db = SessionLocal()
    try:
        task = db.query(AgentTask).filter(AgentTask.task_id == task_id).first()
        if task:
            previous_result = _decode_task_result(task.result)
            if task.status == "completed":
                return {
                    "task_id": task_id,
                    "status": "completed",
                    "skipped": True,
                    "reason": "任务已完成并投递，避免重复执行",
                }
            if task.status != "completed" and previous_result.get("report_path"):
                logger.info(
                    "检测到报告已生成但通知失败，仅重试通知 task_id=%s", task_id
                )
                _update_task(
                    task_id,
                    status="running",
                    retry_count=task.retry_count,
                    error=None,
                )
                return _deliver_scheduled_report(
                    task_id=task_id,
                    task_type=task_type,
                    trigger_type=task.trigger_type,
                    report_path=previous_result.get("report_path"),
                    pushed_policies=previous_result.get("pushed_policies") or [],
                    previous_result=previous_result,
                )
        else:
            task = AgentTask(
                task_id=task_id,
                trigger_type=trigger_type,
                task_type=task_type,
                user_request=prompt,
                status="pending",
                timeout_seconds=timeout_seconds,
            )
            db.add(task)
            db.commit()
    except Exception as exc:
        db.rollback()
        raise RuntimeError(f"创建 Agent Task 失败: {exc}") from exc
    finally:
        db.close()

    if agent_runner:
        runner = agent_runner
    else:
        def runner(run_prompt: str) -> Any:
            return policy_agent.run(
                run_prompt,
                trigger_type=trigger_type,
                task_id=task_id,
                task_type=task_type,
            )

    attempts = max_retries + 1
    last_error: Optional[str] = None

    for attempt in range(1, attempts + 1):
        logger.info(
            "Agent Task 开始 task_id=%s type=%s attempt=%s/%s",
            task_id,
            task_type,
            attempt,
            attempts,
        )
        _update_task(
            task_id,
            status="running",
            started_at=datetime.now(),
            retry_count=attempt - 1,
            error=None,
        )

        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"agent-task-{task_id}")
        try:
            future = executor.submit(runner, prompt)
            result = future.result(timeout=timeout_seconds)
        except FutureTimeoutError:
            last_error = f"任务执行超时（{timeout_seconds}秒）"
            logger.error("Agent Task 超时 task_id=%s: %s", task_id, last_error)
            executor.shutdown(wait=False, cancel_futures=True)
            _update_task(task_id, status="failed", error=last_error, completed_at=datetime.now())
            continue
        except Exception as exc:
            last_error = safe_exception_message(exc)
            logger.error("Agent Task 执行异常 task_id=%s: %s", task_id, last_error)
            executor.shutdown(wait=False, cancel_futures=True)
            _update_task(task_id, status="failed", error=last_error, completed_at=datetime.now())
            continue
        finally:
            if not executor._shutdown:
                executor.shutdown(wait=True)

        state = getattr(result, "state", None)
        report_path = getattr(state, "report_path", None) if state else None
        pushed_policies = getattr(state, "pushed_policies", None) if state else []

        if not bool(getattr(result, "success", False)):
            last_error = str(getattr(result, "error", None) or "Agent 执行失败")
            if report_path:
                # Agent 可能在生成报告后因后续步骤失败；只要报告产物存在，
                # 就优先完成投递闭环，避免重复分析并让用户拿到已生成内容。
                logger.warning(
                    "Agent 返回失败但报告已生成，转交投递 task_id=%s: %s",
                    task_id,
                    redact_sensitive_text(last_error),
                )
                return _deliver_scheduled_report(
                    task_id=task_id,
                    task_type=task_type,
                    trigger_type=trigger_type,
                    report_path=report_path,
                    pushed_policies=pushed_policies,
                    previous_result=previous_result,
                )
            _update_task(
                task_id,
                status="failed",
                error=redact_sensitive_text(last_error),
                completed_at=datetime.now(),
            )
            continue

        return _deliver_scheduled_report(
            task_id=task_id,
            task_type=task_type,
            trigger_type=trigger_type,
            report_path=report_path,
            pushed_policies=pushed_policies,
            previous_result=previous_result,
        )

    error = redact_sensitive_text(last_error or "Agent 执行失败")
    _update_task(task_id, status="failed", error=error, completed_at=datetime.now())
    return {"task_id": task_id, "status": "failed", "error": error}


def create_scheduler() -> BlockingScheduler:
    """创建定时调度器，并将任务模板注册为 Agent Task"""
    init_db()
    scheduler = BlockingScheduler(timezone=ZoneInfo("Asia/Shanghai"))

    for task_type, template in TASK_TEMPLATES.items():
        scheduler.add_job(
            run_agent_task,
            trigger=CronTrigger(**template["cron"]),
            args=[task_type],
            id=task_type,
            name=task_type,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
            replace_existing=True,
        )
        logger.info(f"已注册定时任务 {task_type}: {template['cron']}")

    scheduler.add_job(
        run_source_diagnostics,
        trigger=CronTrigger(hour=7, minute=20),
        id="source_diagnostic",
        name="source_diagnostic",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
        replace_existing=True,
    )
    logger.info("已注册来源稳定性巡检任务 source_diagnostic: 07:20")

    return scheduler


def start_scheduled_tasks() -> None:
    """启动定时调度器，直到进程被手动停止"""
    scheduler = create_scheduler()
    logger.info("定时调度器已启动，等待 Agent Task 触发")
    scheduler.start()
