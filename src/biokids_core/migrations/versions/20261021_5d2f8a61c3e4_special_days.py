"""special days: a day, or a range of days, with rules of its own

Revision ID: 5d2f8a61c3e4
Revises: 077ca4545f23
Created: 2026-10-21 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5d2f8a61c3e4"
down_revision: str | None = "077ca4545f23"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # D-215. Empty on an existing machine: no day was special before there was
    # a way to say so, and every child's usual rules stand.
    op.create_table(
        "day_override",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("child_id", sa.Integer(), nullable=False),
        sa.Column("start_day", sa.Date(), nullable=False),
        sa.Column("end_day", sa.Date(), nullable=False),
        sa.Column("extra_minutes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("quota_minutes", sa.Integer(), nullable=True),
        sa.Column("no_limit", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("weekend_rules", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("label", sa.String(length=40), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("end_day >= start_day", name="ck_day_override_range"),
        sa.CheckConstraint(
            "extra_minutes >= 0 AND extra_minutes <= 1440", name="ck_day_override_extra"
        ),
        sa.CheckConstraint(
            "quota_minutes IS NULL OR (quota_minutes >= 0 AND quota_minutes <= 1440)",
            name="ck_day_override_quota",
        ),
        sa.ForeignKeyConstraint(["child_id"], ["child.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("day_override", schema=None) as batch_op:
        batch_op.create_index(
            "ix_day_override_child_end", ["child_id", "end_day"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("day_override", schema=None) as batch_op:
        batch_op.drop_index("ix_day_override_child_end")
    op.drop_table("day_override")
