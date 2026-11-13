"""数据源传输与网页结构诊断记录。

该表专门记录 WAF、TLS 证书和 DOM 选择器探针结果，与采集结果统计
``source_health`` 分离：采集成功不代表网页结构仍可解析，证书临期也不应
污染“本轮是否抓到数据”的业务语义。
"""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text

from app.database import Base


class SourceDiagnostic(Base):
    """一次来源稳定性探针的可回溯记录。"""

    __tablename__ = "source_diagnostic"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_name = Column(String(100), nullable=False, index=True, comment="来源名称")
    target_url = Column(String(1000), nullable=False, comment="被检查 URL")
    status = Column(
        String(30),
        nullable=False,
        comment=(
            "ok/waf_challenge/tls_expiring/tls_invalid/dom_changed/schema_changed/"
            "unconfigured/not_checked/timeout/dns_error/connection_error/http_error"
        ),
    )
    http_status = Column(Integer, nullable=True, comment="HTTP 状态码")
    latency_ms = Column(Integer, nullable=True, comment="HTTP 请求耗时（毫秒）")
    tls_not_after = Column(DateTime, nullable=True, comment="TLS 证书到期时间（UTC）")
    tls_days_remaining = Column(Integer, nullable=True, comment="证书剩余天数")
    tls_hostname_valid = Column(Boolean, nullable=True, comment="证书主机名校验结果")
    selector = Column(String(500), nullable=True, comment="DOM 结构检查选择器")
    selector_found = Column(Boolean, nullable=True, comment="选择器是否命中；None 表示未配置")
    error = Column(Text, nullable=True, comment="失败原因（已截断）")
    checked_at = Column(DateTime, default=datetime.now, nullable=False, index=True)
