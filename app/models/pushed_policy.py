"""已推送政策台账模型

用于跨日报的内容级去重：同一条政策一旦推送成功，后续周期不再重复进入
主推送列表。通知幂等只防"同一天重复发送"，本表防的是"不同天推送同样内容"。
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, Text

from app.database import Base


class PushedPolicy(Base):
    """政策推送台账"""

    __tablename__ = "pushed_policy"

    id = Column(Integer, primary_key=True, autoincrement=True)
    fingerprint = Column(String(64), nullable=False, unique=True, comment="内容指纹")
    url = Column(String(1000), nullable=True, comment="原文链接")
    title = Column(String(500), nullable=True, comment="政策标题")
    source_name = Column(String(100), nullable=True, comment="来源名称")
    task_id = Column(String(50), nullable=True, comment="首次推送的任务ID")
    first_pushed_at = Column(DateTime, default=datetime.now, comment="首次推送时间")
