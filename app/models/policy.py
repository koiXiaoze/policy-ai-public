"""
政策数据模型
定义 policy 和 analysis 两张核心表
"""

from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, Float, DateTime, ForeignKey, JSON
from sqlalchemy.orm import relationship
from app.database import Base


class Policy(Base):
    """政策文件表"""
    __tablename__ = "policy"

    id = Column(Integer, primary_key=True, autoincrement=True, comment="主键ID")
    title = Column(String(500), nullable=False, comment="政策标题")
    department = Column(String(200), nullable=True, comment="发布部门")
    government_level = Column(String(20), nullable=True, comment="政府级别：国家/省/城市")
    city = Column(String(50), nullable=True, comment="城市（城市级政策时填写）")
    publish_time = Column(DateTime, nullable=True, comment="发布时间")
    url = Column(String(1000), unique=True, nullable=False, comment="原文链接")
    source_name = Column(String(100), nullable=True, comment="来源名称")
    content = Column(Text, nullable=True, comment="政策正文内容")
    summary = Column(String(1000), nullable=True, comment="简要摘要")
    keywords = Column(String(500), nullable=True, comment="关键词，逗号分隔")
    importance_score = Column(Float, default=0.0, comment="重要程度评分 0-100")
    created_time = Column(DateTime, default=datetime.now, comment="记录创建时间")
    updated_time = Column(DateTime, default=datetime.now, onupdate=datetime.now, comment="最后更新时间")

    # 关联分析结果
    analysis = relationship("Analysis", back_populates="policy", uselist=False)

    def __repr__(self):
        return f"<Policy(id={self.id}, title={self.title[:30]})>"


class Analysis(Base):
    """政策分析结果表"""
    __tablename__ = "analysis"

    id = Column(Integer, primary_key=True, autoincrement=True)
    policy_id = Column(Integer, ForeignKey("policy.id"), unique=True, nullable=False, comment="关联政策ID")
    summary = Column(Text, nullable=True, comment="一句话总结")
    plain_explanation = Column(Text, nullable=True, comment="政策大白话解释")
    national_direction = Column(Text, nullable=True, comment="国家释放的信号")
    industry_impact = Column(JSON, nullable=True, comment="影响行业列表（JSON）")
    career_opportunity = Column(JSON, nullable=True, comment="未来机会列表（JSON）")
    personal_advice = Column(JSON, nullable=True, comment="普通大学生建议列表（JSON）")
    risk_analysis = Column(Text, nullable=True, comment="风险分析")
    trend_score = Column(Float, default=0.0, comment="趋势评分 0-100")
    raw_response = Column(Text, nullable=True, comment="AI原始响应")
    created_time = Column(DateTime, default=datetime.now, comment="分析时间")

    # 关联政策
    policy = relationship("Policy", back_populates="analysis")

    def __repr__(self):
        return f"<Analysis(policy_id={self.policy_id}, score={self.trend_score})>"

