"""access requests carry what was asked for: more time, a site, an app

Revision ID: 3e8b51c0d2a7
Revises: 7c1d9e2a4b5f
Created: 2026-10-07 10:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3e8b51c0d2a7"
down_revision: str | None = "7c1d9e2a4b5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Every request filed so far came from the block page, which is a site.
    with op.batch_alter_table("access_request", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("kind", sa.String(length=16), nullable=False, server_default="site")
        )
        batch_op.add_column(
            sa.Column("minutes", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    with op.batch_alter_table("access_request", schema=None) as batch_op:
        batch_op.drop_column("minutes")
        batch_op.drop_column("kind")
