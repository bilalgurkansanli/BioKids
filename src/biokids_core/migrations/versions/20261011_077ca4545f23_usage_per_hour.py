"""screen time per hour of the day, for the insights screen

Revision ID: 077ca4545f23
Revises: c41d8e0f5a92
Created: 2026-10-11 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "077ca4545f23"
down_revision: str | None = "c41d8e0f5a92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # D-206. Empty on an existing machine: the hours a child used the computer
    # before the upgrade were never recorded, and are not guessed now.
    op.create_table(
        "usage_hour",
        sa.Column("child_id", sa.Integer(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("hour", sa.Integer(), nullable=False),
        sa.Column("seconds", sa.Integer(), nullable=False),
        sa.CheckConstraint("hour >= 0 AND hour <= 23", name="ck_usage_hour_hour"),
        sa.ForeignKeyConstraint(["child_id"], ["child.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("child_id", "day", "hour"),
    )
    with op.batch_alter_table("usage_hour", schema=None) as batch_op:
        batch_op.create_index("ix_usage_hour_day", ["day"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("usage_hour", schema=None) as batch_op:
        batch_op.drop_index("ix_usage_hour_day")
    op.drop_table("usage_hour")
