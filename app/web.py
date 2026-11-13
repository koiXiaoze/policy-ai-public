"""最小 FastAPI 服务：提供飞书事件回调入口。"""

import threading
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.agent.async_tasks import recover_interrupted_tasks
from app.channels.feishu import FeishuEventHandler, FeishuSecurityError
from app.channels.wecom import WecomEventHandler, WecomSecurityError
from app.database import init_db
from app.logger import logger


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """服务启动时注册新 ORM 表，清理上次重启遗留的长任务状态，并启动 Scheduler。

    这里只改任务状态，不重跑 Agent、不重发通知：避免重复分析、重复打扰用户。
    Scheduler 放到后台线程（daemon=True），不阻塞 FastAPI 事件循环。
    """
    init_db()
    recover_interrupted_tasks()
    
    # 启动 Scheduler 后台线程：单容器同时跑 HTTP + 定时任务。
    # BlockingScheduler.start() 是阻塞调用，必须放到线程里。
    from app.agent.scheduler import create_scheduler
    scheduler = create_scheduler()
    app.state.scheduler = scheduler  # 存到 app.state，供 /health 检查
    scheduler_thread = threading.Thread(
        target=scheduler.start,
        name="scheduler",
        daemon=True,
    )
    scheduler_thread.start()
    logger.info("Scheduler 后台线程已启动")
    
    yield


app = FastAPI(title="Policy Intelligence Agent", lifespan=lifespan)
feishu_event_handler = FeishuEventHandler()
wecom_event_handler = WecomEventHandler()


@app.get("/health")
def health() -> JSONResponse:
    """部署探针：检查数据库、Scheduler、Agent 与最近通知投递状态。

    故障隔离语义：
    - 数据库、Scheduler、Agent 或最近一批定时报告通知全失败时返回 503；
    - 单个数据源降级/失败只返回 200 + ``degraded``，避免短暂来源问题
      让容器被反复重启；
    - 所有异常文本先脱敏，再写入响应和日志。
    """
    from datetime import datetime, timedelta

    from app.database import SessionLocal
    from app.models.agent_task import AgentTask
    from app.models.notification_record import NotificationRecord
    from app.notifiers.safety import redact_sensitive_text, safe_exception_message
    from app.notifiers.service import _is_stale_pending

    now = datetime.now()
    cutoff = now - timedelta(hours=24)
    scheduled_types = {
        "daily_policy_scan",
        "weekly_policy_trend",
        "monthly_policy_review",
    }
    critical_errors: list[str] = []
    degraded_reasons: list[str] = []
    result = {
        "status": "ok",
        "database": "ok",
        "scheduler": "unknown",
        "last_task": None,
        "agent": "unknown",
        "scheduler_next_run": None,
        "recent_failed_tasks": [],
        "last_scheduled_success_at": None,
        "last_scheduled_failure_at": None,
        "notifications": {
            "success": 0,
            "failed": 0,
            "pending": 0,
            "stale": 0,
        },
        "last_notification_failure": None,
        "degraded_reasons": degraded_reasons,
        "critical_errors": critical_errors,
    }

    def scheduled_notification_query(db):
        """只统计定时报告通知，排除数据源告警等其它通知。"""
        return db.query(NotificationRecord).join(
            AgentTask, AgentTask.task_id == NotificationRecord.task_id
        ).filter(
            AgentTask.task_type.in_(scheduled_types),
            AgentTask.trigger_type == "scheduled",
        )

    # 数据库是其余检查的基础；会话必须统一关闭，异常不得原样暴露。
    db = None
    try:
        db = SessionLocal()
        last_task = db.query(AgentTask).order_by(AgentTask.id.desc()).first()
        if last_task:
            result["last_task"] = {
                "task_id": last_task.task_id,
                "task_type": last_task.task_type,
                "status": last_task.status,
                "completed_at": (
                    last_task.completed_at.isoformat()
                    if last_task.completed_at
                    else None
                ),
            }

        recent_failed_tasks = (
            db.query(AgentTask)
            .filter(
                AgentTask.status == "failed",
                AgentTask.created_at >= cutoff,
            )
            .order_by(AgentTask.created_at.desc(), AgentTask.id.desc())
            .limit(5)
            .all()
        )
        result["recent_failed_tasks"] = [
            {
                "task_id": task.task_id,
                "task_type": task.task_type,
                "completed_at": (
                    task.completed_at.isoformat() if task.completed_at else None
                ),
                "error": redact_sensitive_text(task.error),
            }
            for task in recent_failed_tasks
        ]

        recent_records = (
            scheduled_notification_query(db)
            .filter(NotificationRecord.created_at >= cutoff)
            .order_by(NotificationRecord.created_at.asc(), NotificationRecord.id.asc())
            .all()
        )
        batches: dict[str, list] = {}
        for record in recent_records:
            batches.setdefault(record.task_id, []).append(record)
            if record.status == "success":
                result["notifications"]["success"] += 1
            elif record.status == "failed":
                result["notifications"]["failed"] += 1
            elif record.status == "pending":
                key = "stale" if _is_stale_pending(record, now) else "pending"
                result["notifications"][key] += 1
            else:
                result["notifications"]["failed"] += 1

        # 只看最近一批，避免 24 小时窗口内较早一次成功掩盖最新全失败。
        if batches:
            latest_task_id = max(
                batches,
                key=lambda task_id: max(
                    record.created_at or datetime.min
                    for record in batches[task_id]
                ),
            )
            latest_records = batches[latest_task_id]
            latest_all_failed = bool(latest_records) and all(
                record.status == "failed"
                or (record.status == "pending" and _is_stale_pending(record, now))
                for record in latest_records
            )
            if latest_all_failed:
                critical_errors.append(
                    f"最近定时报告通知全部失败: {latest_task_id}"
                )

        last_success = (
            scheduled_notification_query(db)
            .filter(NotificationRecord.status == "success")
            .order_by(NotificationRecord.updated_at.desc(), NotificationRecord.id.desc())
            .first()
        )
        if last_success:
            timestamp = last_success.sent_at or last_success.updated_at or last_success.created_at
            result["last_scheduled_success_at"] = (
                timestamp.isoformat() if timestamp else None
            )

        last_failure = (
            scheduled_notification_query(db)
            .filter(NotificationRecord.status == "failed")
            .order_by(NotificationRecord.updated_at.desc(), NotificationRecord.id.desc())
            .first()
        )
        if last_failure:
            timestamp = last_failure.updated_at or last_failure.created_at
            result["last_scheduled_failure_at"] = (
                timestamp.isoformat() if timestamp else None
            )
            result["last_notification_failure"] = {
                "task_id": last_failure.task_id,
                "channel": last_failure.channel,
                "status": last_failure.status,
                "occurred_at": result["last_scheduled_failure_at"],
                "error": redact_sensitive_text(last_failure.error_message),
            }
    except Exception as exc:
        safe_error = safe_exception_message(exc)
        result["database"] = f"error: {safe_error}"
        critical_errors.append(f"database: {safe_error}")
    finally:
        if db is not None:
            db.close()

    # Scheduler 不存在、已停止或状态读取失败时，定时闭环不可用。
    scheduler = getattr(app.state, "scheduler", None)
    if scheduler is None:
        result["scheduler"] = "not_initialized"
        critical_errors.append("scheduler: not_initialized")
    else:
        try:
            result["scheduler"] = "running" if scheduler.running else "stopped"
            if not scheduler.running:
                critical_errors.append("scheduler: stopped")

            # 取最早下一次执行时间，避免月任务掩盖每日任务。
            jobs = scheduler.get_jobs()
            next_runs = [job.next_run_time for job in jobs if job.next_run_time]
            if next_runs:
                result["scheduler_next_run"] = min(next_runs).isoformat()
        except Exception as exc:
            safe_error = safe_exception_message(exc)
            result["scheduler"] = f"error: {safe_error}"
            critical_errors.append(f"scheduler: {safe_error}")

    # Agent 工具必须成功加载，否则所有自动化任务都无法执行。
    try:
        from app.agent.tools import tool_registry

        tools_count = len(tool_registry.list_tools())
        result["agent"] = "ok" if tools_count > 0 else "no_tools"
        if tools_count <= 0:
            critical_errors.append("agent: no_tools")
    except Exception as exc:
        safe_error = safe_exception_message(exc)
        result["agent"] = f"error: {safe_error}"
        critical_errors.append(f"agent: {safe_error}")

    # 数据库不可用时，来源健康数据也无法可靠读取；保留核心故障，
    # 避免附加的降级信息掩盖真正根因。
    if result["database"] == "ok":
        try:
            from app.services.source_health import source_health_service

            health_summary = source_health_service.summary()
            result["sources"] = health_summary
            degraded_sources = health_summary.get("degraded_sources") or []
            failing_sources = health_summary.get("failing_sources") or []
            stale_sources = health_summary.get("stale_sources") or []
            if stale_sources:
                degraded_reasons.append(
                    "存在长期未发现新政策的数据源: " + "、".join(stale_sources)
                )
            if degraded_sources:
                degraded_reasons.append(
                    "存在在线采集失败并降级的数据源: " + "、".join(degraded_sources)
                )
            if failing_sources:
                degraded_reasons.append(
                    "存在持续失败的数据源: " + "、".join(failing_sources)
                )
        except Exception as exc:
            safe_error = safe_exception_message(exc)
            result["sources"] = {"error": safe_error}
            degraded_reasons.append(f"数据源健康检查失败: {safe_error}")
            logger.warning("数据源健康检查失败: %s", safe_error)
    else:
        result["sources"] = {"skipped": "database_unavailable"}

    if critical_errors:
        result["status"] = "error"
        status_code = 503
    elif degraded_reasons:
        result["status"] = "degraded"
        status_code = 200
    else:
        status_code = 200

    return JSONResponse(status_code=status_code, content=result)

@app.post("/webhooks/feishu/events")
async def receive_feishu_event(request: Request) -> JSONResponse:
    """接收飞书事件订阅回调。URL 校验和普通事件均返回 JSON。"""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "请求体必须是 JSON"})

    try:
        result = feishu_event_handler.handle(payload)
        return JSONResponse(status_code=200, content=result)
    except FeishuSecurityError as exc:
        logger.warning("飞书回调安全校验失败: %s", exc)
        return JSONResponse(status_code=403, content={"detail": "forbidden"})
    except Exception:
        logger.exception("处理飞书事件失败")
        return JSONResponse(status_code=500, content={"detail": "internal_error"})


@app.get("/webhooks/wecom/events")
async def verify_wecom_url(request: Request) -> JSONResponse:
    """企业微信回调地址校验：验签后回显解密后的 echostr"""
    params = dict(request.query_params)
    try:
        result = wecom_event_handler.verify(params)
    except WecomSecurityError as exc:
        logger.warning("企业微信 URL 校验失败: %s", exc)
        return JSONResponse(status_code=403, content={"detail": "forbidden"})
    except Exception:
        logger.exception("企业微信 URL 校验处理失败")
        return JSONResponse(status_code=500, content={"detail": "internal_error"})

    if result is None:
        return JSONResponse(status_code=400, content={"detail": "缺少 echostr"})
    return JSONResponse(status_code=200, content=result)


@app.post("/webhooks/wecom/events")
async def receive_wecom_event(request: Request) -> JSONResponse:
    """接收企业微信加密消息回调。验签、解密、编排都在处理器内完成。"""
    params = dict(request.query_params)
    try:
        body = await request.body()
        payload = {**params, "xml_body": body.decode("utf-8", errors="replace")}
        result = wecom_event_handler.handle(payload)
        return JSONResponse(status_code=200, content=result)
    except WecomSecurityError as exc:
        logger.warning("企业微信回调安全校验失败: %s", exc)
        return JSONResponse(status_code=403, content={"detail": "forbidden"})
    except Exception:
        logger.exception("处理企业微信事件失败")
        return JSONResponse(status_code=500, content={"detail": "internal_error"})
