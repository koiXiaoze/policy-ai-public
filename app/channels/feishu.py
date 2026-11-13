"""飞书双向消息适配器。

本模块只处理飞书协议、安全校验和消息收发；政策检索、LLM 分析等业务始终由
PolicyAgent 执行。群机器人 Webhook 主动通知仍由 app.notifiers.feishu_notifier 负责。
"""

import hmac
import json
import threading
import time
from datetime import datetime
from hashlib import sha256
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

import httpx

from app.agent.async_tasks import AsyncTaskService
from app.agent.policy_agent import PolicyAgent, policy_agent
from app.channels.base import BaseChannel, ChannelType, IncomingMessage, OutgoingMessage
from app.channels.event_handler import (
    ChannelEventHandler,
    EventIgnored,
    EventSecurityError,
    parse_allowed_users,
)
from app.config import settings
from app.conversations import ConversationService
from app.logger import logger


FEISHU_OPEN_API = "https://open.feishu.cn/open-apis"
FEISHU_MESSAGE_EVENT = "im.message.receive_v1"
FEISHU_TEXT_LIMIT = 4000


class FeishuError(Exception):
    """飞书适配器基础异常"""


class FeishuSecurityError(FeishuError, EventSecurityError):
    """飞书事件未通过安全校验"""


class FeishuEventIgnored(FeishuError, EventIgnored):
    """该事件不应触发 Agent"""


def _split_text(content: str, max_chars: int = FEISHU_TEXT_LIMIT) -> List[str]:
    """按换行优先切分，避免超过飞书文本消息限制。"""
    if not content:
        return []

    chunks: List[str] = []
    current = ""
    for line in content.splitlines(keepends=True) or [content]:
        if current and len(current) + len(line) > max_chars:
            chunks.append(current)
            current = ""
        while len(line) > max_chars:
            chunks.append(line[:max_chars])
            line = line[max_chars:]
        current += line
    if current:
        chunks.append(current)
    return chunks


class FeishuApiClient:
    """飞书自建应用 API 客户端，负责短期 tenant_access_token 缓存。"""

    def __init__(
        self,
        app_id: Optional[str] = None,
        app_secret: Optional[str] = None,
        http_post: Callable[..., Any] = httpx.post,
    ):
        self.app_id = app_id if app_id is not None else settings.feishu_app_id
        self.app_secret = app_secret if app_secret is not None else settings.feishu_app_secret
        self._http_post = http_post
        self._access_token: Optional[str] = None
        self._token_expires_at = 0.0
        self._token_lock = threading.Lock()

    def get_tenant_access_token(self) -> str:
        """按官方自建应用接口获取 tenant_access_token，并预留 60 秒安全窗口。"""
        if not self.app_id or not self.app_secret:
            raise FeishuError("FEISHU_APP_ID 和 FEISHU_APP_SECRET 未配置")

        with self._token_lock:
            if self._access_token and time.time() < self._token_expires_at:
                return self._access_token

            response = self._http_post(
                f"{FEISHU_OPEN_API}/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self.app_secret},
                headers={"Content-Type": "application/json; charset=utf-8"},
                timeout=15.0,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("code") != 0 or not payload.get("tenant_access_token"):
                raise FeishuError(f"获取飞书 tenant_access_token 失败: {payload.get('msg', '未知错误')}")

            self._access_token = str(payload["tenant_access_token"])
            expires_in = int(payload.get("expire", 7200))
            self._token_expires_at = time.time() + max(expires_in - 60, 60)
            return self._access_token

    def send_text(self, chat_id: str, content: str) -> str:
        """使用应用机器人向指定 chat_id 发送一条文本消息。"""
        token = self.get_tenant_access_token()
        response = self._http_post(
            f"{FEISHU_OPEN_API}/im/v1/messages?receive_id_type=chat_id",
            json={
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": content}, ensure_ascii=False),
            },
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            timeout=30.0,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            raise FeishuError(f"发送飞书消息失败: {payload.get('msg', '未知错误')}")
        return str(payload.get("data", {}).get("message_id", ""))


class FeishuChannel(BaseChannel):
    """将飞书消息事件转换为统一消息，并通过自建应用 API 发送回复。"""

    channel_name = ChannelType.FEISHU

    def __init__(self, api_client: Optional[FeishuApiClient] = None):
        self.api_client = api_client or FeishuApiClient()

    def parse_event(self, payload: Mapping[str, Any]) -> IncomingMessage:
        """解析 im.message.receive_v1 的文本消息事件。"""
        header = payload.get("header") or {}
        event = payload.get("event") or {}
        message = event.get("message") or {}
        sender = event.get("sender") or {}
        sender_id = sender.get("sender_id") or {}

        if header.get("event_type") != FEISHU_MESSAGE_EVENT:
            raise FeishuEventIgnored("不是接收消息事件")
        if sender.get("sender_type") and sender.get("sender_type") != "user":
            raise FeishuEventIgnored("忽略非用户发送的消息，避免机器人自触发")
        if message.get("message_type") != "text":
            raise FeishuEventIgnored("当前仅处理文本消息")

        user_id = sender_id.get("open_id") or sender_id.get("user_id") or sender_id.get("union_id")
        message_id = message.get("message_id") or header.get("event_id")
        chat_id = message.get("chat_id")
        if not all(isinstance(item, str) and item.strip() for item in (user_id, message_id, chat_id)):
            raise FeishuError("飞书事件缺少用户、消息或会话标识")

        try:
            content = json.loads(message.get("content") or "{}")
        except json.JSONDecodeError as exc:
            raise FeishuError("飞书文本消息内容不是合法 JSON") from exc
        text = content.get("text")
        if not isinstance(text, str) or not text.strip():
            raise FeishuEventIgnored("飞书文本消息为空")

        created_at = datetime.now()
        raw_timestamp = message.get("create_time")
        if raw_timestamp:
            try:
                created_at = datetime.fromtimestamp(int(raw_timestamp) / 1000)
            except (TypeError, ValueError, OSError):
                logger.warning("飞书消息 create_time 无法解析: %s", raw_timestamp)

        return IncomingMessage(
            channel=ChannelType.FEISHU,
            user_id=user_id,
            conversation_id=chat_id,
            message_id=message_id,
            content=text,
            timestamp=created_at,
            metadata={
                "event_id": header.get("event_id"),
                "chat_type": message.get("chat_type", ""),
                "mentions": message.get("mentions") or [],
                "sender_id": sender_id,
            },
        )

    def send(self, message: OutgoingMessage) -> List[str]:
        """发送回复到原 chat_id；长文本拆分为多条飞书文本消息。"""
        if message.channel != ChannelType.FEISHU:
            raise FeishuError("FeishuChannel 只能发送 feishu 渠道消息")
        chunks = _split_text(message.content)
        if not chunks:
            return []
        return [self.api_client.send_text(message.conversation_id, chunk) for chunk in chunks]


class FeishuEventHandler(ChannelEventHandler):
    """飞书事件入口：安全校验、访问控制、会话持久化和 Agent 编排。

    编排流程在 ChannelEventHandler 中，这里只负责飞书协议细节。
    """

    task_id_prefix = "feishu"

    def __init__(
        self,
        channel: Optional[FeishuChannel] = None,
        conversations: Optional[ConversationService] = None,
        agent: Optional[PolicyAgent] = None,
        async_tasks: Optional[AsyncTaskService] = None,
        verification_token: Optional[str] = None,
        allowed_user_ids: Optional[Iterable[str]] = None,
        bot_open_id: Optional[str] = None,
        command_prefix: Optional[str] = None,
    ):
        verification = (
            verification_token if verification_token is not None else settings.feishu_verification_token
        )
        self.verification_token = verification
        raw_allowed_users: Any = (
            allowed_user_ids
            if allowed_user_ids is not None
            else settings.feishu_allowed_user_ids
        )
        super().__init__(
            channel=channel or FeishuChannel(),
            conversations=conversations or ConversationService(),
            agent=agent or policy_agent,
            async_tasks=async_tasks,
            allowed_user_ids=(
                parse_allowed_users(raw_allowed_users)
                if isinstance(raw_allowed_users, str)
                else raw_allowed_users
            ),
            bot_open_id=bot_open_id if bot_open_id is not None else settings.feishu_bot_open_id,
            command_prefix=command_prefix if command_prefix is not None else settings.feishu_command_prefix,
        )

    def verify(self, payload: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """校验 Verification Token；URL 校验请求就地返回 challenge。"""
        self._validate_event_token(payload)
        if payload.get("type") == "url_verification":
            challenge = payload.get("challenge")
            if not isinstance(challenge, str):
                raise FeishuSecurityError("飞书 URL 校验缺少 challenge")
            return {"challenge": challenge}
        return None

    def parse_event(self, payload: Mapping[str, Any]) -> IncomingMessage:
        """飞书协议解析，细节全部在 FeishuChannel 内。"""
        return self.channel.parse_event(payload)

    def is_group_message(self, incoming: IncomingMessage) -> bool:
        return incoming.metadata.get("chat_type") == "group"

    def _validate_event_token(self, payload: Mapping[str, Any]) -> None:
        """校验事件订阅 Verification Token；未配置时默认拒绝所有请求。"""
        if not self.verification_token:
            raise FeishuSecurityError("FEISHU_VERIFICATION_TOKEN 未配置，拒绝处理飞书回调")
        header = payload.get("header") or {}
        received_token = header.get("token") or payload.get("token")
        if not isinstance(received_token, str) or not hmac.compare_digest(
            received_token, self.verification_token
        ):
            raise FeishuSecurityError("飞书事件 Verification Token 校验失败")

    def prepare_group_message(self, incoming: IncomingMessage) -> Optional[IncomingMessage]:
        """群聊只在命令前缀或明确 @机器人时继续，避免群消息误触发。"""
        if not self.is_group_message(incoming):
            return incoming

        content = incoming.content.strip()
        if self.command_prefix and content.startswith(self.command_prefix):
            cleaned = content[len(self.command_prefix):].strip()
            return incoming.model_copy(update={"content": cleaned}) if cleaned else None

        mentions = incoming.metadata.get("mentions") or []
        mentioned_bot = any(self._mention_matches_bot(mention) for mention in mentions)
        if not (self.bot_open_id and mentioned_bot):
            return None

        for mention in mentions:
            key = mention.get("key") if isinstance(mention, dict) else None
            if isinstance(key, str):
                content = content.replace(key, "")
        content = content.strip()
        return incoming.model_copy(update={"content": content}) if content else None

    def _mention_matches_bot(self, mention: Any) -> bool:
        if not isinstance(mention, dict):
            return False
        identifiers = mention.get("id") or mention.get("user_id") or {}
        if isinstance(identifiers, str):
            return hmac.compare_digest(identifiers, self.bot_open_id)
        if isinstance(identifiers, dict):
            return any(
                isinstance(value, str) and hmac.compare_digest(value, self.bot_open_id)
                for value in identifiers.values()
            )
        return False

    def build_task_id(self, message_id: str) -> str:
        """飞书任务标识：同一 message_id 永远映射到同一个 task_id。"""
        return self._task_id(message_id)

    @staticmethod
    def _task_id(message_id: str) -> str:
        """从飞书 message_id 生成可重试且适合 AgentTask 表的任务标识。"""
        return f"feishu-{sha256(message_id.encode('utf-8')).hexdigest()[:24]}"
