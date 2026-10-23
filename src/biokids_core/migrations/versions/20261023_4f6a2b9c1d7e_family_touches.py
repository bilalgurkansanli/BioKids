"""the family touches: the last round, learning first, notes, a calm screen, birthdays

Revision ID: 4f6a2b9c1d7e
Revises: e3a9c75d1f08
Created: 2026-10-23 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4f6a2b9c1d7e"
down_revision: str | None = "e3a9c75d1f08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # D-230: the last round, three minutes once a day unless a parent changes
    # it, and the one a child took today. D-232: minutes of learning before
    # games open, none on an existing machine - a rule nobody chose is not
    # switched on by an upgrade.
    with op.batch_alter_table("policy", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("finish_up_min", sa.Integer(), nullable=False, server_default="3")
        )
        batch_op.add_column(
            sa.Column("learn_first_min", sa.Integer(), nullable=False, server_default="0")
        )
    with op.batch_alter_table("ledger_day", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("finish_up_s", sa.Integer(), nullable=False, server_default="0")
        )
    # D-236: a calm screen and a birthday, both off until a parent says.
    with op.batch_alter_table("child", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("calm_screen", sa.Boolean(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("birthday", sa.String(length=5), nullable=True))
    # D-233: a parent's "well done" and notes. Empty on an existing machine.
    op.create_table(
        "parent_note",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("child_id", sa.Integer(), nullable=False),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("from_name", sa.String(length=64), server_default="", nullable=False),
        sa.Column("kind", sa.String(length=8), server_default="note", nullable=False),
        sa.Column("text", sa.String(length=160), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("shown_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("kind in ('praise','note')", name="ck_parent_note_kind"),
        sa.ForeignKeyConstraint(["child_id"], ["child.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_parent_note_child_id", "parent_note", ["child_id"])
    op.create_index("ix_parent_note_created_at", "parent_note", ["created_at"])
    # D-240: a recording a child sent from Sesim.
    op.create_table(
        "shared_recording",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("child_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("seconds", sa.Float(), nullable=False),
        sa.Column("audio", sa.LargeBinary(), nullable=False),
        sa.Column("heard_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["child_id"], ["child.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_shared_recording_child_id", "shared_recording", ["child_id"])
    op.create_index("ix_shared_recording_created_at", "shared_recording", ["created_at"])
    # D-241: the reading corner's progress. Empty: nobody has met a letter yet.
    op.create_table(
        "reading_item",
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
    op.drop_table("reading_item")
    op.drop_index("ix_shared_recording_created_at", table_name="shared_recording")
    op.drop_index("ix_shared_recording_child_id", table_name="shared_recording")
    op.drop_table("shared_recording")
    op.drop_index("ix_parent_note_created_at", table_name="parent_note")
    op.drop_index("ix_parent_note_child_id", table_name="parent_note")
    op.drop_table("parent_note")
    with op.batch_alter_table("child", schema=None) as batch_op:
        batch_op.drop_column("birthday")
        batch_op.drop_column("calm_screen")
    with op.batch_alter_table("ledger_day", schema=None) as batch_op:
        batch_op.drop_column("finish_up_s")
    with op.batch_alter_table("policy", schema=None) as batch_op:
        batch_op.drop_column("learn_first_min")
        batch_op.drop_column("finish_up_min")
