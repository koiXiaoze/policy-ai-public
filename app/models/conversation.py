"""会话持久化模型
保存 Channel 层的会话、消息历史和最近Agent状态
"""

from datetime import datetime

from sqlalchemy import JSON, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint

from app.database import Base


class Conversation(Base):
    """会话表：一个 channel + conversation_id 只属于一个用户"""

    __tablename__ = "conversation"
    __table_args__ = (
        UniqueConstraint("channel", "conversation_id", name="uq_conversation_channel_conversation"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    conversation_id = Column(String(200), nullable=False, comment="平台会话ID")
    user_id = Column(String(100), nullable=False, comment="平台用户ID")
    channel = Column(String(20), nullable=False, comment="消息渠道：cli/feishu/wecom/scheduler")
    last_agent_state = Column(JSON, nullable=True, comment="最近一次Agent状态摘要")
    meta = Column(JSON, nullable=True, comment="渠道透传的扩展信息")
    created_at = Column(DateTime, nullable=False, default=datetime.now, comment="创建时间")
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now, comment="更新时间")

    def __repr__(self):
        return f"<Conversation(channel={self.channel}, conversation_id={self.conversation_id}, user_id={self.user_id})>"


class ConversationMessage(Base):
    """会话消息表：按时间保存用户输入和Agent回复"""

    __tablename__ = "conversation_message"
    __table_args__ = (
        UniqueConstraint("channel", "conversation_id", "message_id", name="uq_conversation_message"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    conversation_id = Column(
        Integer,
        ForeignKey("conversation.id", ondelete="CASCADE"),
        nullable=False,
        comment="关联conversation.id",
    )
    channel = Column(String(20), nullable=False, comment="消息渠道：cli/feishu/wecom/scheduler")
    message_id = Column(String(200), nullable=False, comment="平台消息ID或系统生成ID")
    role = Column(String(20), nullable=False, comment="消息角色：user/assistant/system")
    content = Column(Text, nullable=False, comment="消息内容")
    meta = Column(JSON, nullable=True, comment="消息扩展信息")
    created_at = Column(DateTime, nullable=False, default=datetime.now, comment="创建时间")

    def __repr__(self):
        return f"<ConversationMessage(role={self.role}, message_id={self.message_id})>"
