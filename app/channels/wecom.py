"""企业微信自建应用双向消息适配器。

架构约束（来自阶段文档）：企业微信群机器人 Webhook 是单向通知通道，
不能用来接收用户消息。本模块只走企业微信官方"自建应用"链路：
接收端用自建应用的消息回调（URL 校验 GET + 加密 XML POST），
发送端用应用消息 API 按 userid 主动下发。两个方向都是官方能力。

安全要点：回调 URL 的 Token 与 EncodingAESKey 由企业微信后台配置，
服务端必须校验 msg_signature 防止伪造；密钥只从 .env 读取；
用户白名单默认拒绝未授权用户。
"""

import base64
import hashlib
import hmac
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

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


WECOM_API = "https://qyapi.weixin.qq.com/cgi-bin"
WECOM_MESSAGE_LIMIT = 4000


class WecomError(Exception):
    """企业微信适配器基础异常"""


class WecomSecurityError(WecomError, EventSecurityError):
    """回调未通过签名校验或配置缺失"""


class WecomEventIgnored(WecomError, EventIgnored):
    """该事件不应触发 Agent"""


class WecomCrypto:
    """企业微信回调加解密。

    回调报文使用 AES-256-CBC，密钥由 EncodingAESKey 经 base64 解码得到，
    前 16 字节同时用作 IV。签名是 token、timestamp、nonce、echostr
    排序后拼接再取 SHA1。
    """

    def __init__(self, token: str, encoding_aes_key: str):
        if not token:
            raise WecomSecurityError("WECOM_TOKEN 未配置，拒绝处理企业微信回调")
        if not encoding_aes_key or len(encoding_aes_key) != 43:
            raise WecomSecurityError("WECOM_ENCODING_AES_KEY 未配置或长度不正确（应为43位）")

        self.token = token
        self.key = base64.b64decode(encoding_aes_key + "=")
        self.iv = self.key[:16]

    def signature(self, timestamp: str, nonce: str, echo: str) -> str:
        """按企业微信规则计算签名"""
        parts = sorted([self.token, str(timestamp), str(nonce), echo])
        return hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()

    def verify(self, signature: str, timestamp: str, nonce: str, echo: str) -> bool:
        """用 hmac.compare_digest 比对签名，避免时序侧信道"""
        return hmac.compare_digest(
            self.signature(timestamp, nonce, echo), str(signature or "")
        )

    def decrypt(self, encrypted: str) -> str:
        """解密回调报文体，返回明文 XML"""
        try:
            raw = base64.b64decode(encrypted)
            decryptor = Cipher(algorithms.AES(self.key), modes.CBC(self.iv)).decryptor()
            plain = decryptor.update(raw) + decryptor.finalize()
        except Exception as exc:
            raise WecomSecurityError(f"企业微信回调解密失败: {exc}") from exc

        padding = plain[-1]
        if padding < 1 or padding > 32:
            raise WecomSecurityError("企业微信回调填充非法")
        return plain[:-padding].decode("utf-8", errors="replace")

    def encrypt(self, plaintext: str) -> str:
        """加密内容，URL 校验回显与被动回复都需要"""
        data = plaintext.encode("utf-8")
        pad = 32 - len(data) % 32
        data += bytes([pad]) * pad
        encryptor = Cipher(algorithms.AES(self.key), modes.CBC(self.iv)).encryptor()
        return base64.b64encode(encryptor.update(data) + encryptor.finalize()).decode("utf-8")


def _parse_xml_fields(xml_text: str) -> Dict[str, str]:
    """把企业微信 XML 回调解析成扁平字典"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise WecomError(f"企业微信回调不是合法 XML: {exc}") from exc
    return {child.tag: (child.text or "") for child in root}


def _split_text(content: str, max_chars: int = WECOM_MESSAGE_LIMIT) -> List[str]:
    """按换行优先切分，避免超过企业微信单条文本消息长度限制"""
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


class WecomApiClient:
    """企业微信应用 API 客户端，负责 access_token 缓存"""

    def __init__(
        self,
        corp_id: Optional[str] = None,
        corp_secret: Optional[str] = None,
        agent_id: Optional[int] = None,
        http_get: Callable[..., Any] = httpx.get,
        http_post: Callable[..., Any] = httpx.post,
    ):
        self.corp_id = corp_id if corp_id is not None else settings.wecom_corp_id
        self.corp_secret = corp_secret if corp_secret is not None else settings.wecom_corp_secret
        self.agent_id = agent_id if agent_id is not None else settings.wecom_agent_id
        self._http_get = http_get
        self._http_post = http_post
        self._access_token: Optional[str] = None
        self._token_expires_at = 0.0

    def get_access_token(self) -> str:
        """获取 access_token，并预留 60 秒安全窗口避免边界失效"""
        if not self.corp_id or not self.corp_secret:
            raise WecomError("WECOM_CORP_ID 和 WECOM_CORP_SECRET 未配置")

        if self._access_token and time.time() < self._token_expires_at:
            return self._access_token

        response = self._http_get(
            f"{WECOM_API}/gettoken",
            params={"corpid": self.corp_id, "corpsecret": self.corp_secret},
            timeout=15.0,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errcode") != 0 or not payload.get("access_token"):
            raise WecomError(
                f"获取企业微信 access_token 失败: "
                f"{payload.get('errmsg', '未知错误')}({payload.get('errcode')})"
            )

        self._access_token = str(payload["access_token"])
        expires_in = int(payload.get("expires_in", 7200))
        self._token_expires_at = time.time() + max(expires_in - 60, 60)
        return self._access_token

    def send_text(self, user_id: str, content: str) -> int:
        """向指定成员的应用会话发送文本消息"""
        token = self.get_access_token()
        response = self._http_post(
            f"{WECOM_API}/message/send",
            params={"access_token": token},
            json={
                "touser": user_id,
                "msgtype": "text",
                "agentid": self.agent_id,
                "text": {"content": content},
            },
            timeout=30.0,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errcode") != 0:
            raise WecomError(
                f"发送企业微信消息失败: {payload.get('errmsg', '未知错误')}"
                f"({payload.get('errcode')})"
            )
        return int(payload.get("msgid", 0) or 0)


class WecomChannel(BaseChannel):
    """企业微信消息适配器：只做协议转换和主动发送"""

    channel_name = ChannelType.WECOM

    def __init__(self, api_client: Optional[WecomApiClient] = None):
        self.api_client = api_client or WecomApiClient()

    def parse_event(self, payload: Mapping[str, Any]) -> IncomingMessage:
        """解析企业微信 text 类型消息回调"""
        fields = _parse_xml_fields(str(payload.get("xml_body") or ""))

        msg_type = fields.get("MsgType")
        if msg_type != "text":
            raise WecomEventIgnored(f"当前仅处理文本消息，收到: {msg_type}")

        user_id = fields.get("FromUserName")
        message_id = fields.get("MsgId")
        content = (fields.get("Content") or "").strip()
        if not user_id or not message_id:
            raise WecomError("企业微信回调缺少发送人或消息标识")
        if not content:
            raise WecomEventIgnored("企业微信文本消息为空")

        created_at = datetime.now()
        raw_create_time = fields.get("CreateTime")
        if raw_create_time:
            try:
                created_at = datetime.fromtimestamp(int(raw_create_time))
            except (TypeError, ValueError, OSError):
                logger.warning("企业微信消息 CreateTime 无法解析: %s", raw_create_time)

        # 应用单聊用 userid 标识会话；群聊场景带 ChatId。
        chat_id = fields.get("ChatId") or user_id
        return IncomingMessage(
            channel=ChannelType.WECOM,
            user_id=user_id,
            conversation_id=chat_id,
            message_id=message_id,
            content=content,
            timestamp=created_at,
            metadata={
                "chat_id": fields.get("ChatId"),
                "chat_type": "group" if fields.get("ChatId") else "single",
                "to_user": fields.get("ToUserName"),
                "agent_id": fields.get("AgentID"),
            },
        )

    def send(self, message: OutgoingMessage) -> List[int]:
        """按 userid 发送回复；长文本拆分为多条"""
        if message.channel != ChannelType.WECOM:
            raise WecomError("WecomChannel 只能发送 wecom 渠道消息")
        chunks = _split_text(message.content)
        if not chunks:
            return []
        return [self.api_client.send_text(message.conversation_id, chunk) for chunk in chunks]


class WecomEventHandler(ChannelEventHandler):
    """企业微信回调入口：官方自建应用链路，验签 + 白名单 + Agent 编排"""

    task_id_prefix = "wecom"

    def __init__(
        self,
        channel: Optional[WecomChannel] = None,
        conversations: Optional[ConversationService] = None,
        agent: Optional[PolicyAgent] = None,
        async_tasks: Optional[AsyncTaskService] = None,
        token: Optional[str] = None,
        encoding_aes_key: Optional[str] = None,
        allowed_user_ids: Optional[Any] = None,
        command_prefix: Optional[str] = None,
    ):
        self.token = token if token is not None else settings.wecom_token
        self.encoding_aes_key = (
            encoding_aes_key if encoding_aes_key is not None else settings.wecom_encoding_aes_key
        )
        raw_allowed_users: Any = (
            allowed_user_ids
            if allowed_user_ids is not None
            else settings.wecom_allowed_user_ids
        )
        super().__init__(
            channel=channel or WecomChannel(),
            conversations=conversations or ConversationService(),
            agent=agent or policy_agent,
            async_tasks=async_tasks,
            allowed_user_ids=(
                parse_allowed_users(raw_allowed_users)
                if isinstance(raw_allowed_users, str)
                else raw_allowed_users
            ),
            command_prefix=(
                command_prefix if command_prefix is not None else settings.wecom_command_prefix
            ),
        )

    def crypto(self) -> WecomCrypto:
        """按需构造加解密器，签名验证在 URL 校验和消息回调两处都要走"""
        return WecomCrypto(self.token, self.encoding_aes_key)

    def verify(self, payload: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """校验 msg_signature；URL 校验请求就地回显 echostr

        企业微信的 URL 校验是 GET 带 echostr，POST 消息回调带加密 XML。
        两者都必须验签；回显明文由 web 层按企业微信要求输出。
        """
        signature = payload.get("msg_signature")
        timestamp = payload.get("timestamp")
        nonce = payload.get("nonce")
        echo = payload.get("echostr") or payload.get("xml_body") or ""
        if not signature or not timestamp or not nonce:
            raise WecomSecurityError("企业微信回调缺少签名参数")

        crypto = self.crypto()
        if not crypto.verify(signature, timestamp, nonce, echo):
            raise WecomSecurityError("企业微信回调 msg_signature 校验失败")

        if payload.get("echostr"):
            return {"echostr": crypto.decrypt(str(payload["echostr"]))}
        return None

    def parse_event(self, payload: Mapping[str, Any]) -> IncomingMessage:
        """解密 XML 后交给渠道适配器解析"""
        plain = self.crypto().decrypt(str(payload.get("xml_body") or ""))
        return self.channel.parse_event({"xml_body": plain})

    def is_group_message(self, incoming: IncomingMessage) -> bool:
        """企业微信群里带 ChatId，单聊没有"""
        return incoming.metadata.get("chat_type") == "group"

    def prepare_group_message(self, incoming: IncomingMessage) -> Optional[IncomingMessage]:
        """群聊只响应命令前缀，避免普通闲聊误触发 Agent"""
        if not self.is_group_message(incoming):
            return incoming

        content = incoming.content.strip()
        if self.command_prefix and content.startswith(self.command_prefix):
            cleaned = content[len(self.command_prefix):].strip()
            return incoming.model_copy(update={"content": cleaned}) if cleaned else None
        return None
