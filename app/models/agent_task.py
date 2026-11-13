"""Agent 任务模型
记录定时任务和用户任务的统一执行状态
"""

from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime
from app.database import Base


class AgentTask(Base):
    """Agent 任务表"""
    __tablename__ = "agent_task"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    task_id = Column(String(50), nullable=False, unique=True, comment="任务ID")
    trigger_type = Column(String(20), nullable=False, default="user", comment="触发类型：user/cli/feishu/wecom/scheduled")
    task_type = Column(String(50), nullable=False, comment="任务类型：daily/weekly/monthly/user_query/long_task")
    user_request = Column(Text, nullable=False, comment="触发任务的自然语言指令")
    status = Column(String(20), nullable=False, default="pending", comment="状态：pending/running/completed/failed")
    result = Column(Text, nullable=True, comment="任务执行结果摘要")
    error = Column(Text, nullable=True, comment="错误信息")
    # 长任务字段：异步执行需要记住"结果要发回哪里"，否则进程重启后无处投递。
    channel = Column(String(20), nullable=True, comment="来源渠道：cli/feishu/wecom/scheduler")
    conversation_id = Column(String(200), nullable=True, comment="目标会话ID，长任务完成后回发到此会话")
    user_id = Column(String(100), nullable=True, comment="触发任务的用户ID")
    retry_count = Column(Integer, nullable=False, default=0, comment="重试次数")
    timeout_seconds = Column(Integer, nullable=False, default=600, comment="单次执行超时秒数")
    created_at = Column(DateTime, default=datetime.now, comment="创建时间")
    started_at = Column(DateTime, nullable=True, comment="开始时间")
    completed_at = Column(DateTime, nullable=True, comment="完成时间")

    def __repr__(self):
        return f"<AgentTask(task_id={self.task_id}, task_type={self.task_type}, status={self.status})>"
