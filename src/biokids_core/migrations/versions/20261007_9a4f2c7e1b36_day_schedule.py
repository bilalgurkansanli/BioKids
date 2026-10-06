"""a schedule per day of the week, over the weekday/weekend pair

Revision ID: 9a4f2c7e1b36
Revises: 3e8b51c0d2a7
Created: 2026-10-07 14:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9a4f2c7e1b36"
down_revision: str | None = "3e8b51c0d2a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Empty for every existing child: their weekday and weekend rules stand.
    with op.batch_alter_table("policy", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("schedule_json", sa.Text(), nullable=False, server_default="{}")
        )


def downgrade() -> None:
    with op.batch_alter_table("policy", schema=None) as batch_op:
        batch_op.drop_column("schedule_json")
