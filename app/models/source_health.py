"""数据源健康记录模型

每次采集都会为每个来源写入一条健康记录，用于：
1. 回答"某来源最近一次成功是什么时候、抓到多少条"；
2. 识别连续失败的来源，避免静默退化（例如武汉曾被禁用、十堰曾被封禁）。

设计为"每次采集追加一条"，便于按时间回溯；查询时取每个来源的最新一条。
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, Text, UniqueConstraint

from app.database import Base


class SourceHealth(Base):
    """数据源健康记录"""

    __tablename__ = "source_health"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_name = Column(String(100), nullable=False, comment="来源名称")
    status = Column(String(20), nullable=False, default="unknown", comment="ok/empty/degraded/failed")
    item_count = Column(Integer, nullable=False, default=0, comment="本次抓取条数")
    newest_publish_time = Column(DateTime, nullable=True, comment="抓到的最新政策日期")
    error = Column(Text, nullable=True, comment="失败原因")
    error_type = Column(String(40), nullable=True, comment="稳定错误类型")
    consecutive_failures = Column(Integer, nullable=False, default=0, comment="连续失败次数")
    task_id = Column(String(50), nullable=True, comment="关联任务ID")
    checked_at = Column(DateTime, default=datetime.now, comment="检查时间")
