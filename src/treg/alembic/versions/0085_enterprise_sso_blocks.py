"""Keep minimal manual access blocks across WorkOS connection replacement.

Revision ID: 0085
Revises: 0084
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0085"
down_revision: str | Sequence[str] | None = "0084"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "enterprisessoblock",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("org.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("org_id", "user_id", name="uq_enterprise_sso_block_user"),
    )
    op.create_index("ix_enterprisessoblock_org_id", "enterprisessoblock", ["org_id"])
    op.create_index("ix_enterprisessoblock_user_id", "enterprisessoblock", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_enterprisessoblock_user_id", table_name="enterprisessoblock")
    op.drop_index("ix_enterprisessoblock_org_id", table_name="enterprisessoblock")
    op.drop_table("enterprisessoblock")
