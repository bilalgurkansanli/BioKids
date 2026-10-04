"""the assistant: per-child switches, and the question an alert is about

Revision ID: 7c1d9e2a4b5f
Revises: ef25a8819f04
Created: 2026-10-03 22:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7c1d9e2a4b5f"
down_revision: str | None = "ef25a8819f04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Both off for every existing child: an upgrade never turns the assistant on.
    with op.batch_alter_table("policy", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "assistant_enabled", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
        batch_op.add_column(
            sa.Column(
                "assistant_voice", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
    with op.batch_alter_table("ai_alert", schema=None) as batch_op:
        batch_op.add_column(sa.Column("text", sa.Text(), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("ai_alert", schema=None) as batch_op:
        batch_op.drop_column("text")
    with op.batch_alter_table("policy", schema=None) as batch_op:
        batch_op.drop_column("assistant_voice")
        batch_op.drop_column("assistant_enabled")
