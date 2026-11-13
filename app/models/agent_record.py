"""
Agent 执行记录模型
记录每次 Agent 工具调用的完整执行轨迹
"""

from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime
from app.database import Base


class AgentExecutionRecord(Base):
    """Agent 工具执行记录表"""
    __tablename__ = "agent_execution_record"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    task_id = Column(String(50), nullable=False, comment="任务ID")
    user_request = Column(Text, nullable=False, comment="用户原始请求")
    trigger_type = Column(String(20), nullable=False, default="user", comment="触发类型：user/scheduled")
    tool_name = Column(String(100), nullable=False, comment="工具名称")
    tool_args = Column(Text, nullable=True, comment="工具参数（JSON序列化）")
    tool_result_summary = Column(Text, nullable=True, comment="工具结果摘要")
    status = Column(String(20), nullable=False, default="pending", comment="执行状态：pending/running/success/failed")
    execution_time = Column(Integer, nullable=True, comment="执行耗时（毫秒）")
    error_message = Column(Text, nullable=True, comment="错误信息")
    created_time = Column(DateTime, default=datetime.now, comment="记录创建时间")

    def __repr__(self):
        return f"<AgentExecutionRecord(task_id={self.task_id}, tool={self.tool_name}, status={self.status})>"
