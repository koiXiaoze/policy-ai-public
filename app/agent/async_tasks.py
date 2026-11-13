"""长任务异步执行

机器人收到"分析过去三年湖北人工智能政策"这类请求时，同步等待会占住 HTTP 连接，
而趋势分析要跨年检索 + 多次 LLM 调用，耗时不可控。本模块的职责只有两件事：

1. 立刻把任务丢进后台队列，让渠道线程马上能回执；
2. 后台跑完 Agent 后，把结果主动发回原 conversation_id。

不碰 Agent Core、Scheduler 和任何工具，只做编排。

执行后端选型说明（三选一，本项目取 A）：

- A 进程内单 worker 线程池：零新依赖，不动 Scheduler，符合"单实例单 Scheduler"
  的硬约束。代价是进程重启会丢掉尚未开始执行的队列。
- B Redis + RQ/Celery：真解耦、可横向扩展，但要引入新基础设施和部署依赖，
  与本阶段约束冲突。
- C 复用 APScheduler 跑用户任务：虽然已依赖该库，但 APScheduler 是定时调度器，
  拿它当通用队列会让"定时任务"和"用户任务"混在同一套状态里，更难排查。

选 A 之后，重启丢失队列的问题由 recover_interrupted_tasks() 显式标记为失败，
用户能看到"任务未完成"，而不是永远停在 running。
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from app.channels.base import IncomingMessage, OutgoingMessage
from app.channels.registry import get_channel
from app.database import SessionLocal
from app.logger import logger
from app.models.agent_task import AgentTask


class AsyncTaskError(Exception):
    """异步任务基础异常"""


class AsyncTaskQueueFull(AsyncTaskError):
    """后台队列已满，拒绝新任务"""


#: 后台长任务的任务类型。Scheduler 的 daily/weekly 不受影响。
LONG_TASK_TYPE = "long_task"

#: 进程启动时判定为"异常中断"的状态。
INTERRUPTED_STATUSES = ("queued", "running")


def _note(prefix: str, task_id: str) -> str:
    """生成可读的备注文本，留在 AgentTask.error 里便于排查"""
    return f"{prefix} (task_id={task_id})"


class AsyncTaskService:
    """进程内单 worker 长任务队列"""

    def __init__(
        self,
        conversations: Optional[Any] = None,
        agent_runner: Optional[Callable[..., Any]] = None,
        channel_factory: Callable[[str], Any] = get_channel,
        session_factory: Callable[[], Any] = SessionLocal,
        max_workers: int = 1,
        max_queue: int = 32,
        timeout_seconds: int = 600,
    ):
        if max_workers < 1:
            raise ValueError("max_workers 至少为 1")

        self._conversations = conversations
        self._agent_runner = agent_runner
        self._channel_factory = channel_factory
        self._session_factory = session_factory
        self._max_queue = max_queue
        self._timeout_seconds = timeout_seconds

        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="long-task",
        )
        self._lock = threading.Lock()
        # 投递目标：task_id -> 渠道/会话/用户。保留在内存里用于拦截同进程重复提交。
        self._deliveries: Dict[str, Dict[str, str]] = {}
        self._pending = 0

    def submit(
        self,
        message: IncomingMessage,
        task_id: str,
        context: Optional[List[Dict[str, str]]] = None,
        on_accepted: Optional[Callable[[], None]] = None,
    ) -> Dict[str, Any]:
        """受理一条长任务并立即返回，不等待 Agent 执行结束

        Args:
            message: 已通过安全校验的标准化用户消息。
            task_id: 调用方生成的稳定任务标识，用于幂等和投递消息ID。
            context: 已隔离的会话上下文，Agent Core 不自行读取全库历史。
            on_accepted: 受理成功后的回执钩子，在后台线程启动之前调用。
                渠道用它先发"已收到"，否则结果可能比回执更早到达用户。

        Raises:
            AsyncTaskQueueFull: 队列已满，应回一条拒绝文案而不是让用户干等。
        """
        if not task_id or not str(task_id).strip():
            raise AsyncTaskError("task_id 不能为空")

        with self._lock:
            if task_id in self._deliveries:
                logger.info(f"长任务已受理过，跳过重复提交 task_id={task_id}")
                return {"status": "duplicate", "task_id": task_id}
            if self._pending >= self._max_queue:
                raise AsyncTaskQueueFull(
                    f"当前已有 {self._pending} 个长任务在排队，请稍后再试"
                )

            delivery = {
                "channel": message.channel.value,
                "conversation_id": message.conversation_id,
                "user_id": message.user_id,
                "message_id": message.message_id,
            }
            self._deliveries[task_id] = delivery
            self._pending += 1

        self._record_task(message, task_id)
        # 回执必须早于执行：否则后台先跑完，用户会先看到结果再看到"已收到"。
        if on_accepted is not None:
            try:
                on_accepted()
            except Exception as exc:
                logger.error(f"长任务回执回调失败 task_id={task_id}: {exc}")
        self._executor.submit(
            self._run,
            dict(message.to_payload()),
            task_id,
            list(context or []),
            delivery,
        )
        logger.info(
            f"长任务已进入后台队列 task_id={task_id} channel={delivery['channel']} "
            f"conversation_id={delivery['conversation_id']}"
        )
        return {"status": "accepted", "task_id": task_id}

    def pending_count(self) -> int:
        """当前排队 + 执行中的任务数，供健康检查使用"""
        with self._lock:
            return self._pending

    def shutdown(self, wait: bool = False) -> None:
        """停止接收新任务；wait=True 时等当前任务跑完（进程退出用）"""
        self._executor.shutdown(wait=wait)

    def _run(
        self,
        payload: Dict[str, Any],
        task_id: str,
        context: List[Dict[str, str]],
        delivery: Dict[str, str],
    ) -> None:
        """后台线程主体：跑 Agent，然后回发结果。任何异常都不能让线程静默死掉。"""
        started_at = datetime.now()
        try:
            message = IncomingMessage.model_validate(payload)
            runner = self._agent_runner or self._default_runner
            result = runner(
                message.content,
                trigger_type=message.trigger_type.value,
                task_id=task_id,
                task_type=LONG_TASK_TYPE,
                context=context,
            )
            elapsed = int((datetime.now() - started_at).total_seconds())
            if elapsed > self._timeout_seconds:
                logger.warning(
                    f"长任务执行超过约定时长 task_id={task_id} "
                    f"elapsed={elapsed}s timeout={self._timeout_seconds}s"
                )

            answer = getattr(result, "answer", "") or ""
            success = bool(getattr(result, "success", True))
            self._deliver(task_id, delivery, answer, success=success, elapsed=elapsed)
        except Exception as exc:
            logger.exception(
                f"长任务执行失败 task_id={task_id} channel={delivery['channel']}: {exc}"
            )
            self._mark_unfinished_failed(task_id, exc)
            self._deliver(
                task_id,
                delivery,
                f"任务执行失败：{exc}",
                success=False,
                elapsed=int((datetime.now() - started_at).total_seconds()),
            )
        finally:
            with self._lock:
                self._pending = max(self._pending - 1, 0)

    def _deliver(
        self,
        task_id: str,
        delivery: Dict[str, str],
        content: str,
        success: bool = True,
        elapsed: int = 0,
    ) -> None:
        """把后台结果发回原会话并落库

        投递失败只记录日志，不抛异常：AgentTask 状态由 Agent Core 维护，
        不应该因为"发不出消息"而被改写。
        """
        channel_name = delivery["channel"]
        try:
            channel = self._channel_factory(channel_name)
        except Exception as exc:
            logger.error(
                f"长任务结果无法投递，渠道未接入 task_id={task_id} channel={channel_name}: {exc}"
            )
            return

        reply = OutgoingMessage(
            channel=channel_name,
            conversation_id=delivery["conversation_id"],
            content=content,
            reply_to=delivery["message_id"],
            metadata={"task_id": task_id, "async": True, "success": success},
        )

        try:
            sent_ids = channel.send(reply)
        except Exception as exc:
            logger.error(f"长任务结果发送失败 task_id={task_id} channel={channel_name}: {exc}")
            return

        # 消息没发出去就不写历史，否则会话里会出现用户从未收到的回复。
        try:
            self._get_conversations().record_reply(
                reply,
                user_id=delivery["user_id"],
                message_id=f"async-{task_id}",
                meta={"task_id": task_id, "sent_message_ids": sent_ids, "elapsed_seconds": elapsed},
            )
        except Exception as exc:
            logger.error(f"长任务结果写入会话失败 task_id={task_id}: {exc}")
            return

        logger.info(
            f"长任务结果已回发 task_id={task_id} channel={channel_name} "
            f"success={success} elapsed={elapsed}s"
        )

    def _get_conversations(self) -> Any:
        """延迟获取会话服务，避免模块导入期就绑定数据库"""
        if self._conversations is None:
            from app.conversations import ConversationService

            self._conversations = ConversationService(session_factory=self._session_factory)
        return self._conversations

    @staticmethod
    def _default_runner(user_query: str, **kwargs: Any) -> Any:
        """默认执行器：调用全局 PolicyAgent，与 CLI/飞书共用同一个 Agent Core"""
        from app.agent.policy_agent import policy_agent

        return policy_agent.run(user_query, **kwargs)

    def _record_task(self, message: IncomingMessage, task_id: str) -> None:
        """写入 queued 状态和投递目标

        投递目标必须提前落库：进程重启后即使队列丢了，也能查到"本该发到哪"。
        """
        db = self._session_factory()
        try:
            task = db.query(AgentTask).filter(AgentTask.task_id == task_id).first()
            if task is None:
                task = AgentTask(task_id=task_id)
                db.add(task)
            task.trigger_type = message.trigger_type.value
            task.task_type = LONG_TASK_TYPE
            task.user_request = message.content
            task.status = "queued"
            task.channel = message.channel.value
            task.conversation_id = message.conversation_id
            task.user_id = message.user_id
            task.timeout_seconds = self._timeout_seconds
            task.created_at = task.created_at or datetime.now()
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.error(f"写入长任务记录失败 task_id={task_id}: {exc}")
        finally:
            db.close()

    def _mark_unfinished_failed(self, task_id: str, exc: BaseException) -> None:
        """兜底把仍是 queued/running 的任务标记为失败

        正常路径下 Agent Core 自己会写 completed/failed，这里只处理
        "异常发生在 Agent Core 之外"的场景，例如消息反序列化失败。
        带上状态判断是为了不覆盖 Agent Core 已经写好的终态。
        """
        db = self._session_factory()
        try:
            task = db.query(AgentTask).filter(AgentTask.task_id == task_id).first()
            if task is None or task.status not in INTERRUPTED_STATUSES:
                return
            task.status = "failed"
            task.error = _note(f"任务异常中断: {exc}", task_id)
            task.completed_at = datetime.now()
            db.commit()
            logger.warning(f"长任务状态兜底标记为失败 task_id={task_id}: {exc}")
        except Exception as mark_exc:
            db.rollback()
            logger.error(f"长任务状态兜底写入失败 task_id={task_id}: {mark_exc}")
        finally:
            db.close()


def recover_interrupted_tasks(
    session_factory: Callable[[], Any] = SessionLocal,
) -> int:
    """进程启动时把上次残留的长任务标记为失败

    只处理 task_type=long_task 且状态仍为 queued/running 的记录，
    不碰 Scheduler 的 daily/weekly，也不碰正常完成的短查询任务。

    Returns:
        标记为失败的记录数。
    """
    db = session_factory()
    recovered = 0
    try:
        tasks = (
            db.query(AgentTask)
            .filter(
                AgentTask.task_type == LONG_TASK_TYPE,
                AgentTask.status.in_(INTERRUPTED_STATUSES),
            )
            .all()
        )
        now = datetime.now()
        for task in tasks:
            task.status = "failed"
            task.error = _note("服务重启导致任务中断，未完成", task.task_id)
            task.completed_at = now
            recovered += 1
        if recovered:
            db.commit()
            logger.warning(f"已标记 {recovered} 条被重启打断的长任务为失败")
    except Exception as exc:
        db.rollback()
        logger.error(f"恢复中断长任务失败: {exc}")
    finally:
        db.close()
    return recovered
