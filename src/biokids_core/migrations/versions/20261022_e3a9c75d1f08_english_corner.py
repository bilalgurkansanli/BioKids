"""the English corner's progress

Revision ID: e3a9c75d1f08
Revises: b7e41d2c9a63
Created: 2026-10-22 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e3a9c75d1f08"
down_revision: str | None = "b7e41d2c9a63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # D-225. Empty on an existing machine: no child has met a word yet.
    op.create_table(
        "english_item",
        sa.Column("child_id", sa.Integer(), nullable=False),
        sa.Column("item", sa.String(length=64), nullable=False),
        sa.Column("box", sa.Integer(), server_default="1", nullable=False),
        sa.Column("due", sa.Date(), nullable=False),
        sa.Column("first_day", sa.Date(), nullable=False),
        sa.Column("right", sa.Integer(), server_default="0", nullable=False),
        sa.Column("wrong", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_day", sa.Date(), nullable=False),
        sa.ForeignKeyConstraint(["child_id"], ["child.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("child_id", "item"),
    )


def downgrade() -> None:
    op.drop_table("english_item")
