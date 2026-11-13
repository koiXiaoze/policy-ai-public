"""add source health error type

Revision ID: 7f3c2a91b6d4
Revises: d5158aeb6720
Create Date: 2026-11-10

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "7f3c2a91b6d4"
down_revision: str | Sequence[str] | None = "d5158aeb6720"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """为来源健康记录增加稳定错误类型。"""
    op.add_column(
        "source_health",
        sa.Column(
            "error_type",
            sa.String(length=40),
            nullable=True,
            comment="稳定错误类型",
        ),
    )


def downgrade() -> None:
    """回退稳定错误类型列。"""
    op.drop_column("source_health", "error_type")
