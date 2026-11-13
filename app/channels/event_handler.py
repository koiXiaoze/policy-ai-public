"""渠道事件处理器的公共编排

飞书和企业微信的回调流程完全一致：

校验 -> 解析 -> 白名单 -> 群控 -> 落库去重 -> 构造上下文 -> 长任务或同步执行 -> 回发

差异只在平台细节（签名算法、消息体格式、URL 校验响应格式、发送 API）。
把这些差异留成钩子，流程只写一份，避免长任务逻辑在两个渠道里各改各的。

这里不含任何政策检索、LLM 分析逻辑，Agent Core 依然是唯一的执行者。
"""

import threading
from abc import ABC, abstractmethod
from hashlib import sha256
from typing import Any, ClassVar, Dict, Iterable, List, Mapping, Optional, Set

from app.agent.async_tasks import AsyncTaskQueueFull, AsyncTaskService
from app.agent.policy_agent import PolicyAgent
from app.agent.task_router import build_ack_message, build_rejected_message, is_long_task
from app.channels.base import BaseChannel, IncomingMessage, OutgoingMessage
from app.conversations import ConversationService
from app.logger import logger


class EventIgnored(Exception):
    """该事件不应触发 Agent（不是用户消息、空内容等）"""


class EventSecurityError(Exception):
    """事件未通过安全校验"""


class ChannelEventHandler(ABC):
    """所有渠道事件处理器的基类"""

    #: 平台消息 ID 到 AgentTask 任务 ID 的前缀
    task_id_prefix: ClassVar[str] = "async"

    def __init__(
        self,
        channel: BaseChannel,
        conversations: ConversationService,
        agent: PolicyAgent,
        async_tasks: Optional[AsyncTaskService] = None,
        allowed_user_ids: Iterable[str] = (),
        bot_open_id: Optional[str] = None,
        command_prefix: Optional[str] = None,
    ):
        self.channel = channel
        self.conversations = conversations
        self.agent = agent
        self.async_tasks = async_tasks
        self.allowed_user_ids: Set[str] = {
            item.strip() for item in allowed_user_ids if item and item.strip()
        }
        self.bot_open_id = bot_open_id
        self.command_prefix = command_prefix
        # 第一次遇到长任务时才建队列，避免没启用异步时也占一个后台线程。
        self._async_lock = threading.Lock()

    # ---------- 平台细节钩子 ----------

    @abstractmethod
    def verify(self, payload: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """校验事件来源

        Returns:
            非 None 表示该事件已就地处理完，直接作为 HTTP 响应返回；
            None 表示继续走正常消息流程。

        Raises:
            EventSecurityError: 签名或 token 校验失败。
        """
        raise NotImplementedError

    @abstractmethod
    def parse_event(self, payload: Mapping[str, Any]) -> IncomingMessage:
        """把平台原始消息转换为统一消息

        Raises:
            EventIgnored: 该消息不应触发 Agent。
        """
        raise NotImplementedError

    def prepare_group_message(self, incoming: IncomingMessage) -> Optional[IncomingMessage]:
        """群聊过滤；返回 None 表示忽略。默认放行，由子类按平台群聊规则覆盖。"""
        return incoming

    def is_group_message(self, incoming: IncomingMessage) -> bool:
        """默认单群不分群；企业微信和飞书各自覆盖。"""
        return False

    # ---------- 固定流程 ----------

    def handle(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        """处理一条回调事件，返回可直接作为 HTTP 响应的字典"""
        early = self.verify(payload)
        if early is not None:
            return early

        try:
            incoming = self.parse_event(payload)
        except EventIgnored as exc:
            logger.info(f"{self.task_id_prefix} 事件已忽略: {exc}")
            return {"status": "ignored", "reason": str(exc)}

        if incoming.user_id not in self.allowed_user_ids:
            logger.warning(
                f"{self.task_id_prefix} 用户不在白名单，已忽略 user_id={incoming.user_id}"
            )
            return {"status": "ignored", "reason": "user_not_allowed"}

        prepared = self.prepare_group_message(incoming)
        if prepared is None:
            return {"status": "ignored", "reason": "group_control"}

        # 先保存输入，且只让同一 message_id 产生一条最终回复。
        saved = self.conversations.record_incoming(prepared)
        response_message_id = f"agent-{prepared.message_id}"
        history = self.conversations.get_messages(
            channel=prepared.channel.value,
            conversation_id=prepared.conversation_id,
            user_id=prepared.user_id,
            limit=20,
        )
        if saved["deduplicated"] and any(
            item["message_id"] == response_message_id for item in history
        ):
            return {"status": "ignored", "reason": "duplicate"}

        context = [
            {"role": item["role"], "content": item["content"]}
            for item in history
            if item["message_id"] != prepared.message_id
        ][-10:]
        task_id = self.build_task_id(prepared.message_id)

        # 长任务在后台跑，这里必须立刻返回，否则平台回调会超时并被重投。
        if self.maybe_handle_long_task(prepared, task_id, context, response_message_id):
            return {"status": "accepted", "task_id": task_id}

        result = self.agent.run(
            prepared.content,
            trigger_type=self.trigger_type,
            task_id=task_id,
            task_type="user_query",
            context=context,
        )

        reply = OutgoingMessage.for_reply(
            prepared,
            result.answer,
            metadata={"task_id": task_id, "success": result.success},
        )
        sent_message_ids = self.channel.send(reply)
        self.conversations.record_reply(
            reply,
            user_id=prepared.user_id,
            message_id=response_message_id,
            meta={"message_ids": sent_message_ids, "task_id": task_id},
        )
        if result.state:
            self.conversations.update_last_agent_state(
                channel=prepared.channel.value,
                conversation_id=prepared.conversation_id,
                user_id=prepared.user_id,
                state=result.state,
            )
        return {"status": "replied", "task_id": task_id, "message_ids": sent_message_ids}

    @property
    def trigger_type(self) -> str:
        """Agent 任务归属的触发类型，必须与 channel 对应"""
        return self.channel.channel_name.value

    def build_task_id(self, message_id: str) -> str:
        """从平台 message_id 生成可重试、可回溯的任务标识

        同一 message_id 永远得到同一个 task_id，平台重投不会重复执行。
        """
        return f"{self.task_id_prefix}-{sha256(message_id.encode('utf-8')).hexdigest()[:24]}"

    def maybe_handle_long_task(
        self,
        incoming: IncomingMessage,
        task_id: str,
        context: List[Dict[str, str]],
        ack_message_id: str,
    ) -> bool:
        """识别长任务并转后台执行

        Returns:
            True 表示这条消息已走异步路径，调用方不要再同步执行 Agent；
            False 表示短查询，交给同步逻辑。
        """
        if not is_long_task(incoming.content):
            return False

        try:
            service = self.get_async_service()
        except Exception as exc:
            logger.error(f"长任务队列不可用 task_id={task_id}: {exc}")
            return False

        try:
            result = service.submit(
                incoming,
                task_id=task_id,
                context=context,
                on_accepted=lambda: self.reply_now(
                    incoming, build_ack_message(task_id), ack_message_id, task_id
                ),
            )
        except AsyncTaskQueueFull as exc:
            self.reply_now(incoming, build_rejected_message(str(exc)), ack_message_id, task_id)
            return True
        except Exception as exc:
            logger.exception(f"长任务提交失败 task_id={task_id}: {exc}")
            self.reply_now(
                incoming, build_rejected_message("后台任务提交失败"), ack_message_id, task_id
            )
            return True

        if result.get("status") == "duplicate":
            logger.info(f"长任务重复提交已忽略 task_id={task_id}")
        return True

    def reply_now(
        self,
        incoming: IncomingMessage,
        content: str,
        message_id: str,
        task_id: str,
    ) -> None:
        """立刻回一条短消息，失败只记日志，不能让回调整体失败"""
        reply = OutgoingMessage.for_reply(
            incoming,
            content,
            metadata={"task_id": task_id, "async": True},
        )
        try:
            sent_ids = self.channel.send(reply)
        except Exception as exc:
            logger.error(f"{self.task_id_prefix} 回执发送失败 task_id={task_id}: {exc}")
            return
        try:
            self.conversations.record_reply(
                reply,
                user_id=incoming.user_id,
                message_id=message_id,
                meta={"message_ids": sent_ids, "task_id": task_id},
            )
        except Exception as exc:
            logger.error(f"{self.task_id_prefix} 回执写入会话失败 task_id={task_id}: {exc}")

    def get_async_service(self) -> AsyncTaskService:
        """线程安全地懒加载长任务队列"""
        if self.async_tasks is None:
            with self._async_lock:
                if self.async_tasks is None:
                    self.async_tasks = AsyncTaskService(conversations=self.conversations)
        return self.async_tasks


def parse_allowed_users(raw: str) -> Set[str]:
    """把逗号分隔的 .env 配置解析成用户 ID 集合"""
    return {item.strip() for item in (raw or "").split(",") if item.strip()}
