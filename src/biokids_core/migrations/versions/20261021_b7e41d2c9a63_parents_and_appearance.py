"""a second parent, and how each screen is drawn

Revision ID: b7e41d2c9a63
Revises: 077ca4545f23
Created: 2026-10-21 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7e41d2c9a63"
down_revision: str | None = "077ca4545f23"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # D-218: a parent who is removed keeps their row, so the audit log can still
    # name them and their id is never reused. D-217: text size and contrast for
    # each parent, and the same plus the reading font for each child. Every
    # existing row gets the defaults, which draw exactly what was drawn before.
    with op.batch_alter_table("parent", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("text_size", sa.String(length=8), nullable=False, server_default="normal")
        )
        batch_op.add_column(
            sa.Column("high_contrast", sa.Boolean(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("removed_at", sa.DateTime(), nullable=True))
    with op.batch_alter_table("child", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("text_size", sa.String(length=8), nullable=False, server_default="normal")
        )
        batch_op.add_column(
            sa.Column("high_contrast", sa.Boolean(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("reading_font", sa.Boolean(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    # Before D-218 the first parent row was the one that signed in. A removed
    # parent has no PIN left, so if it stayed first nobody could; the rows that
    # still sign in come first again without them.
    op.execute("DELETE FROM parent WHERE removed_at IS NOT NULL")
    with op.batch_alter_table("child", schema=None) as batch_op:
        batch_op.drop_column("reading_font")
        batch_op.drop_column("high_contrast")
        batch_op.drop_column("text_size")
    with op.batch_alter_table("parent", schema=None) as batch_op:
        batch_op.drop_column("removed_at")
        batch_op.drop_column("high_contrast")
        batch_op.drop_column("text_size")
