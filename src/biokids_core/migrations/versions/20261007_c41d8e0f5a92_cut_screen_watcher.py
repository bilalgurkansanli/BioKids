"""cuts for v1: the screen watcher's switches and columns, the voice-cloning table

Revision ID: c41d8e0f5a92
Revises: 9a4f2c7e1b36
Created: 2026-10-07 16:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c41d8e0f5a92"
down_revision: str | None = "9a4f2c7e1b36"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # D-180. Nothing ever set them: the watcher was never built.
    with op.batch_alter_table("policy", schema=None) as batch_op:
        batch_op.drop_column("ai_enabled")
        batch_op.drop_column("ai_block_enabled")
        batch_op.drop_column("ai_timeout_min")
    # The screenshot path and the VLM's free-text reasons; the assistant's
    # alerts use the category, the reason key and the question.
    with op.batch_alter_table("ai_alert", schema=None) as batch_op:
        batch_op.drop_column("evidence_path")
        batch_op.drop_column("reason_tr")
        batch_op.drop_column("reason_en")
        batch_op.drop_column("url")
    # D-181: voice cloning is cut; a parent's own voice is a recording (D-174).
    op.drop_table("voice_profile")


def downgrade() -> None:
    op.create_table(
        "voice_profile",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("persona", sa.String(length=8), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("locale", sa.String(length=8), nullable=False),
        sa.Column("model_path", sa.String(length=512), nullable=False),
        sa.Column("label", sa.String(length=64), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("persona in ('zio','sio')", name="ck_voice_profile_persona"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("persona", "kind", "locale", name="uq_voice_profile"),
    )
    with op.batch_alter_table("ai_alert", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("url", sa.String(length=1024), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("reason_en", sa.Text(), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("reason_tr", sa.Text(), nullable=False, server_default="")
        )
        batch_op.add_column(sa.Column("evidence_path", sa.String(length=512), nullable=True))
    with op.batch_alter_table("policy", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("ai_timeout_min", sa.Integer(), nullable=False, server_default="30")
        )
        batch_op.add_column(
            sa.Column(
                "ai_block_enabled", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
        batch_op.add_column(
            sa.Column("ai_enabled", sa.Boolean(), nullable=False, server_default=sa.false())
        )
