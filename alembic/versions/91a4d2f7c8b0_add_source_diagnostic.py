"""add source diagnostic

Revision ID: 91a4d2f7c8b0
Revises: 7f3c2a91b6d4
Create Date: 2026-11-12

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "91a4d2f7c8b0"
down_revision: str | Sequence[str] | None = "7f3c2a91b6d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """记录 WAF、TLS 与 DOM 结构探针结果。"""
    op.create_table(
        "source_diagnostic",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_name", sa.String(length=100), nullable=False, comment="来源名称"),
        sa.Column("target_url", sa.String(length=1000), nullable=False, comment="被检查 URL"),
        sa.Column("status", sa.String(length=30), nullable=False, comment="ok/waf_challenge/tls_expiring/tls_invalid/dom_changed/schema_changed/unconfigured/not_checked/timeout/dns_error/connection_error/http_error"),
        sa.Column("http_status", sa.Integer(), nullable=True, comment="HTTP 状态码"),
        sa.Column("latency_ms", sa.Integer(), nullable=True, comment="HTTP 请求耗时（毫秒）"),
        sa.Column("tls_not_after", sa.DateTime(), nullable=True, comment="TLS 证书到期时间（UTC）"),
        sa.Column("tls_days_remaining", sa.Integer(), nullable=True, comment="证书剩余天数"),
        sa.Column("tls_hostname_valid", sa.Boolean(), nullable=True, comment="证书主机名校验结果"),
        sa.Column("selector", sa.String(length=500), nullable=True, comment="DOM 结构检查选择器"),
        sa.Column("selector_found", sa.Boolean(), nullable=True, comment="选择器是否命中"),
        sa.Column("error", sa.Text(), nullable=True, comment="失败原因"),
        sa.Column("checked_at", sa.DateTime(), nullable=False, comment="检查时间"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_source_diagnostic_source_name",
        "source_diagnostic",
        ["source_name"],
        unique=False,
    )
    op.create_index(
        "ix_source_diagnostic_checked_at",
        "source_diagnostic",
        ["checked_at"],
        unique=False,
    )


def downgrade() -> None:
    """回退诊断记录表。"""
    op.drop_index("ix_source_diagnostic_checked_at", table_name="source_diagnostic")
    op.drop_index("ix_source_diagnostic_source_name", table_name="source_diagnostic")
    op.drop_table("source_diagnostic")
