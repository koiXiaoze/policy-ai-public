"""通知记录模型
用于通知幂等、状态和重试追踪
"""

from datetime import datetime
from sqlalchemy import Column, DateTime, Integer, JSON, String, Text
from app.database import Base


class NotificationRecord(Base):
    """通知发送记录表"""
    __tablename__ = "notification_record"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    task_id = Column(String(50), nullable=False, comment="关联任务ID")
    channel = Column(String(20), nullable=False, comment="通知渠道：email/wechat/feishu")
    idempotency_key = Column(String(200), nullable=False, unique=True, comment="幂等键")
    status = Column(String(20), nullable=False, default="pending", comment="状态：pending/success/failed")
    chunks = Column(Integer, nullable=False, default=0, comment="总分片数")
    sent_chunks = Column(Integer, nullable=False, default=0, comment="已成功发送的分片数")
    retry_count = Column(Integer, nullable=False, default=0, comment="重试次数")
    max_retries = Column(Integer, nullable=False, default=2, comment="最大重试次数")
    error_message = Column(Text, nullable=True, comment="错误信息")
    meta = Column(JSON, nullable=True, comment="通知上下文")
    created_at = Column(DateTime, default=datetime.now, comment="创建时间")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, comment="更新时间")
    sent_at = Column(DateTime, nullable=True, comment="完成时间")

    def __repr__(self):
        return f"<NotificationRecord(task_id={self.task_id}, channel={self.channel}, status={self.status})>"
