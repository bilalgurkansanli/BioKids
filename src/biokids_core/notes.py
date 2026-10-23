"""A parent's "well done" and short notes to a child (D-233).

The panel was all control: lock, switch off, limit. This is its warm side. A
parent at work taps "Aferin" on the child's card, or writes one sentence -
"Akşam altıda geliyorum, seni seviyorum" - and the child's screen shows it,
Zio reads it out, and the parent's card says when it was seen. Kept with the
day's other records for `retention.activity_days` (`retention.prune`); the
audit log records that a note was sent, never its words.

The daemon is the only writer: it stores a note and tells the child's launcher
in the same step (`OperationsMixin.send_note`), so a note is never shown twice
nor lost between the two.
"""

from __future__ import annotations

import datetime as dt
import unicodedata
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from biokids_core.db.models import ParentNote, utcnow

KINDS: Final = ("praise", "note")

TEXT_MAX: Final = 140
"""One sentence a child of four hears and a child of eight reads. The table holds
a little more, so a sentence at the limit is never cut by the database."""

SHOWN_FOR: Final = dt.timedelta(days=7)
"""A note older than this is not shown any more: "I'll be home at six" from last
week is not news, and the child may not have been at the computer since."""

UNSEEN_SHOWN: Final = 3
"""At most this many waiting notes are handed to the launcher, oldest first."""


def clean_text(text: str) -> str:
    """One line of plain text: no control characters, spaces folded, cut at the
    limit on a word where it can be."""
    printable = "".join(
        ch if unicodedata.category(ch)[0] != "C" else " " for ch in (text or "")
    )
    folded = " ".join(printable.split())
    if len(folded) <= TEXT_MAX:
        return folded
    cut = folded[:TEXT_MAX]
    space = cut.rfind(" ")
    return (cut[:space] if space > TEXT_MAX // 2 else cut).rstrip(" ,;:")


def add(
    session: Session,
    child_id: int,
    kind: str,
    text: str,
    *,
    parent_id: int | None,
    from_name: str,
) -> ParentNote:
    """Store a note. Raises ValueError for a kind that is not one, or a note with
    no words. Does not commit: the caller tells the child in the same step."""
    if kind not in KINDS:
        raise ValueError(f"no such kind of note: {kind}")
    cleaned = clean_text(text)
    if kind == "note" and not cleaned:
        raise ValueError("a note needs words")
    note = ParentNote(
        child_id=child_id,
        parent_id=parent_id,
        from_name=" ".join((from_name or "").split())[:64],
        kind=kind,
        text=cleaned if kind == "note" else "",
    )
    session.add(note)
    session.flush()
    return note


def unseen(session: Session, child_id: int, now: dt.datetime | None = None) -> list[ParentNote]:
    """The notes waiting for this child, oldest first, from the last week."""
    now = now or utcnow()
    return list(
        session.scalars(
            select(ParentNote)
            .where(
                ParentNote.child_id == child_id,
                ParentNote.shown_at.is_(None),
                ParentNote.created_at >= now - SHOWN_FOR,
            )
            .order_by(ParentNote.created_at, ParentNote.id)
            .limit(UNSEEN_SHOWN)
        ).all()
    )


def mark_seen(session: Session, child_id: int, note_id: int) -> bool:
    """The child's screen showed it. Only the calling child's own note."""
    note = session.get(ParentNote, note_id)
    if note is None or note.child_id != child_id:
        return False
    if note.shown_at is None:
        note.shown_at = utcnow()
    return True


def as_card(note: ParentNote) -> dict[str, Any]:
    """What the launcher draws: never the parent's id, only the name the child knows."""
    return {"id": note.id, "kind": note.kind, "text": note.text, "from": note.from_name}


def sent_today(
    session: Session, child_ids: list[int], start: dt.datetime
) -> dict[int, list[ParentNote]]:
    """The notes sent to each child since `start` (naive UTC), for their card on Today."""
    if not child_ids:
        return {}
    rows = session.scalars(
        select(ParentNote)
        .where(ParentNote.child_id.in_(child_ids), ParentNote.created_at >= start)
        .order_by(ParentNote.created_at, ParentNote.id)
    ).all()
    found: dict[int, list[ParentNote]] = {}
    for row in rows:
        found.setdefault(row.child_id, []).append(row)
    return found
