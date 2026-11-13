"""Conversation Service

为 CLI、飞书、企业微信等入口提供持久化会话上下文。
服务只负责会话存取，不执行政策搜索、LLM分析或报告生成。
"""

from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional
from uuid import uuid4

from app.channels import IncomingMessage, OutgoingMessage
from app.database import SessionLocal
from app.logger import logger
from app.models.conversation import Conversation, ConversationMessage


class ConversationError(Exception):
    """会话服务基础异常"""


class ConversationAccessError(ConversationError):
    """用户访问了不属于自己的会话"""


class ConversationService:
    """持久化会话与消息历史"""

    def __init__(self, session_factory: Callable[[], Any] = SessionLocal):
        self._session_factory = session_factory

    def ensure_conversation(
        self,
        channel: str,
        conversation_id: str,
        user_id: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> int:
        """确保会话存在，并校验会话归属"""
        self._validate_identity(channel, conversation_id, user_id)

        db = self._session_factory()
        try:
            conversation = self._find_conversation(db, channel, conversation_id)
            if conversation:
                if conversation.user_id != user_id:
                    raise ConversationAccessError("conversation_id已属于其他用户")
                return conversation.id

            conversation = Conversation(
                channel=channel,
                conversation_id=conversation_id,
                user_id=user_id,
                meta=meta or {},
            )
            db.add(conversation)
            db.commit()
            db.refresh(conversation)
            logger.info(
                f"会话已创建 channel={channel} conversation_id={conversation_id} user_id={user_id}"
            )
            return conversation.id
        except ConversationAccessError:
            db.rollback()
            raise
        except Exception as exc:
            db.rollback()
            logger.error(f"创建会话失败 channel={channel} conversation_id={conversation_id}: {exc}")
            raise ConversationError(f"创建会话失败: {exc}") from exc
        finally:
            db.close()

    def record_incoming(self, message: IncomingMessage) -> Dict[str, Any]:
        """记录用户输入，并返回消息摘要"""
        conversation_id = self.ensure_conversation(
            channel=message.channel.value,
            conversation_id=message.conversation_id,
            user_id=message.user_id,
            meta=message.metadata,
        )
        return self._append_message(
            conversation_id=conversation_id,
            channel=message.channel.value,
            message_id=message.message_id,
            role="user",
            content=message.content,
            meta={
                **message.metadata,
                "trigger_type": message.trigger_type.value,
                "message_timestamp": message.timestamp.isoformat(),
            },
        )

    def record_reply(
        self,
        reply: OutgoingMessage,
        user_id: str,
        message_id: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """记录Agent回复，message_id可由task_id生成用于幂等"""
        conversation_id = self.ensure_conversation(
            channel=reply.channel.value,
            conversation_id=reply.conversation_id,
            user_id=user_id,
        )
        return self._append_message(
            conversation_id=conversation_id,
            channel=reply.channel.value,
            message_id=message_id or f"assistant-{uuid4().hex[:12]}",
            role="assistant",
            content=reply.content,
            meta={**(meta or {}), **(reply.metadata or {})},
        )

    def append_message(
        self,
        channel: str,
        conversation_id: str,
        user_id: str,
        role: str,
        content: str,
        message_id: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """通用消息追加入口，供后续长任务或系统提示复用"""
        if role not in {"user", "assistant", "system"}:
            raise ConversationError(f"不支持的消息角色: {role}")

        conversation_pk = self.ensure_conversation(
            channel=channel,
            conversation_id=conversation_id,
            user_id=user_id,
        )
        return self._append_message(
            conversation_id=conversation_pk,
            channel=channel,
            message_id=message_id or f"{role}-{uuid4().hex[:12]}",
            role=role,
            content=content,
            meta=meta or {},
        )

    def get_messages(
        self,
        channel: str,
        conversation_id: str,
        user_id: str,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """按时间正序返回指定用户指定会话的消息"""
        self._validate_identity(channel, conversation_id, user_id)
        if limit <= 0:
            return []

        db = self._session_factory()
        try:
            conversation = self._find_conversation(db, channel, conversation_id)
            if not conversation or conversation.user_id != user_id:
                return []

            messages = (
                db.query(ConversationMessage)
                .filter(ConversationMessage.conversation_id == conversation.id)
                .order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc())
                .limit(limit)
                .all()
            )
            return [self._message_to_dict(item) for item in reversed(messages)]
        finally:
            db.close()

    def get_context(
        self,
        channel: str,
        conversation_id: str,
        user_id: str,
        max_messages: int = 10,
        max_chars: int = 8000,
    ) -> List[Dict[str, str]]:
        """生成OpenAI messages格式的最近上下文，并按字符数截断"""
        messages = self.get_messages(
            channel=channel,
            conversation_id=conversation_id,
            user_id=user_id,
            limit=max_messages,
        )

        context: List[Dict[str, str]] = []
        total_chars = 0
        for item in reversed(messages):
            content = item["content"]
            total_chars += len(content)
            if total_chars > max_chars and context:
                break
            context.append({"role": item["role"], "content": content})

        return list(reversed(context))

    def update_last_agent_state(
        self,
        channel: str,
        conversation_id: str,
        user_id: str,
        state: Any,
    ) -> None:
        """保存最近一次Agent状态摘要"""
        conversation_pk = self.ensure_conversation(channel, conversation_id, user_id)
        serialized_state = self._serialize_state(state)

        db = self._session_factory()
        try:
            conversation = db.query(Conversation).filter(Conversation.id == conversation_pk).first()
            if not conversation:
                raise ConversationError("会话不存在")
            conversation.last_agent_state = serialized_state
            conversation.updated_at = datetime.now()
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.error(f"更新会话Agent状态失败 conversation_id={conversation_id}: {exc}")
            raise ConversationError(f"更新会话Agent状态失败: {exc}") from exc
        finally:
            db.close()

    def get_last_agent_state(
        self,
        channel: str,
        conversation_id: str,
        user_id: str,
    ) -> Optional[Dict[str, Any]]:
        """读取指定用户指定会话的最近Agent状态"""
        self._validate_identity(channel, conversation_id, user_id)
        db = self._session_factory()
        try:
            conversation = self._find_conversation(db, channel, conversation_id)
            if not conversation or conversation.user_id != user_id:
                return None
            return conversation.last_agent_state
        finally:
            db.close()

    def _append_message(
        self,
        conversation_id: int,
        channel: str,
        message_id: str,
        role: str,
        content: str,
        meta: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """追加消息；同一message_id重复写入时幂等返回已有记录"""
        if not content.strip():
            raise ConversationError("消息内容不能为空")

        db = self._session_factory()
        try:
            existing = (
                db.query(ConversationMessage)
                .filter(
                    ConversationMessage.conversation_id == conversation_id,
                    ConversationMessage.message_id == message_id,
                )
                .first()
            )
            if existing:
                result = self._message_to_dict(existing)
                result["deduplicated"] = True
                return result

            message = ConversationMessage(
                conversation_id=conversation_id,
                channel=channel,
                message_id=message_id,
                role=role,
                content=content,
                meta=meta or {},
            )
            db.add(message)

            conversation = db.query(Conversation).filter(Conversation.id == conversation_id).first()
            if conversation:
                conversation.updated_at = datetime.now()

            db.commit()
            db.refresh(message)
            logger.debug(
                f"会话消息已保存 channel={channel} conversation_pk={conversation_id} role={role}"
            )
            result = self._message_to_dict(message)
            result["deduplicated"] = False
            return result
        except ConversationError:
            db.rollback()
            raise
        except Exception as exc:
            db.rollback()
            logger.error(f"保存会话消息失败 channel={channel} conversation_pk={conversation_id}: {exc}")
            raise ConversationError(f"保存会话消息失败: {exc}") from exc
        finally:
            db.close()

    def _find_conversation(self, db: Any, channel: str, conversation_id: str) -> Optional[Conversation]:
        """按渠道和平台会话ID查找会话"""
        return (
            db.query(Conversation)
            .filter(
                Conversation.channel == channel,
                Conversation.conversation_id == conversation_id,
            )
            .first()
        )

    @staticmethod
    def _validate_identity(channel: str, conversation_id: str, user_id: str) -> None:
        """在访问数据库前先拒绝空标识，防止无效会话写入"""
        if not all(isinstance(value, str) and value.strip() for value in (channel, conversation_id, user_id)):
            raise ConversationError("channel、conversation_id和user_id均不能为空")

    @staticmethod
    def _serialize_state(state: Any) -> Dict[str, Any]:
        """兼容dataclass、Pydantic模型和普通字典状态"""
        if isinstance(state, Mapping):
            return dict(state)
        if hasattr(state, "to_dict"):
            return state.to_dict()
        if hasattr(state, "model_dump"):
            return state.model_dump(mode="json")
        raise ConversationError("last_agent_state必须为dict、dataclass.to_dict()或Pydantic模型")

    @staticmethod
    def _message_to_dict(message: ConversationMessage) -> Dict[str, Any]:
        """将ORM对象转换为稳定的普通字典"""
        return {
            "id": message.id,
            "conversation_id": message.conversation_id,
            "message_id": message.message_id,
            "role": message.role,
            "content": message.content,
            "meta": message.meta or {},
            "created_at": message.created_at.isoformat() if message.created_at else None,
        }
