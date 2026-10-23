"""D-233: a parent's "well done" and short notes to a child.

The panel's warm side. What matters: the daemon stores a note and tells the
child in one step, a note waits for a child who is not at the computer and is
shown once, the parent's card says whether it was seen, the audit log says that
a note was sent and never what it said, and nothing old is shown or kept.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from biokids_core import notes, retention
from biokids_core.clock import FakeClock
from biokids_core.config import Config
from biokids_core.db import AuditLog, Child, Database
from biokids_core.db.models import ParentNote, utcnow
from biokids_core.ipc import EventKind
from biokids_daemon.bus import EventBus
from biokids_daemon.enforcer import Enforcer, RecordingRunner
from biokids_daemon.sessions import FakeSessionSource, SessionWatcher
from biokids_daemon.supervisor import Supervisor
from test_shell import LAUNCHER, FakeShellDaemon, a_state

# ---------------------------------------------------------------- the words


@pytest.mark.parametrize(
    ("typed", "kept"),
    [
        ("  Akşam   altıda geliyorum  ", "Akşam altıda geliyorum"),
        ("satır\nsatır\tsekme", "satır satır sekme"),
        ("\x00\x1b[31mkırmızı", "[31mkırmızı"),
        ("", ""),
    ],
)
def test_a_note_is_one_line_of_plain_text(typed: str, kept: str) -> None:
    assert notes.clean_text(typed) == kept


def test_a_long_note_is_cut_on_a_word() -> None:
    long = "seni çok seviyorum " * 20
    cut = notes.clean_text(long)
    assert len(cut) <= notes.TEXT_MAX
    assert cut.endswith(("seni", "çok", "seviyorum"))


def test_a_note_needs_words_and_a_kind(session: Session, child: Child) -> None:
    with pytest.raises(ValueError):
        notes.add(session, child.id, "note", "   ", parent_id=None, from_name="Anne")
    with pytest.raises(ValueError):
        notes.add(session, child.id, "letter", "Merhaba", parent_id=None, from_name="Anne")
    praise = notes.add(session, child.id, "praise", "ignored", parent_id=None, from_name="Anne")
    assert praise.text == "", "a well done carries no words of its own"


# ------------------------------------------------------- waiting and seen


def test_waiting_notes_are_shown_oldest_first_and_once(session: Session, child: Child) -> None:
    first = notes.add(session, child.id, "praise", "", parent_id=1, from_name="Anne")
    second = notes.add(
        session, child.id, "note", "Seni seviyorum", parent_id=1, from_name="Anne"
    )
    session.commit()
    assert [n.id for n in notes.unseen(session, child.id)] == [first.id, second.id]
    assert notes.mark_seen(session, child.id, first.id)
    session.commit()
    assert [n.id for n in notes.unseen(session, child.id)] == [second.id]


def test_a_child_cannot_mark_a_siblings_note(session: Session, child: Child) -> None:
    sibling = Child(unix_user="deniz", display_name="Deniz", birth_year=2018, locale="tr")
    session.add(sibling)
    session.flush()
    theirs = notes.add(session, sibling.id, "note", "Merhaba", parent_id=1, from_name="Baba")
    session.commit()
    assert notes.mark_seen(session, child.id, theirs.id) is False
    assert notes.mark_seen(session, child.id, 99999) is False
    assert theirs.shown_at is None


def test_last_weeks_note_is_not_news(session: Session, child: Child) -> None:
    old = notes.add(
        session, child.id, "note", "Altıda geliyorum", parent_id=1, from_name="Anne"
    )
    old.created_at = utcnow() - dt.timedelta(days=8)
    session.commit()
    assert notes.unseen(session, child.id) == []


def test_at_most_a_few_notes_are_handed_over(session: Session, child: Child) -> None:
    for n in range(6):
        notes.add(session, child.id, "note", f"Not {n}", parent_id=1, from_name="Anne")
    session.commit()
    assert len(notes.unseen(session, child.id)) == notes.UNSEEN_SHOWN


def test_notes_go_with_the_days_other_records(
    session: Session, child: Child, config: Config
) -> None:
    kept = notes.add(session, child.id, "note", "Yeni", parent_id=1, from_name="Anne")
    gone = notes.add(session, child.id, "note", "Eski", parent_id=1, from_name="Anne")
    gone.created_at = utcnow() - dt.timedelta(days=config.retention.activity_days + 1)
    session.commit()
    deleted = retention.prune(session, config)
    session.commit()
    assert deleted["parent_note"] == 1
    assert [n.id for n in session.scalars(select(ParentNote)).all()] == [kept.id]


# ------------------------------------------------------------------- daemon


@pytest.fixture
def supervisor(
    db: Database, source: FakeSessionSource, clock: FakeClock, config: Config, child: Child
) -> Supervisor:
    return Supervisor(
        database=db,
        bus=EventBus(queue_size=256, history_size=256),
        watcher=SessionWatcher(source),
        enforcer=Enforcer(config, RecordingRunner()),
        clock=clock,
        config=config,
    )


@pytest.fixture
def source() -> FakeSessionSource:
    return FakeSessionSource()


def _events(supervisor: Supervisor, kind: EventKind) -> list[dict[str, Any]]:
    return [e.payload for e in supervisor.bus.history if e.kind == str(kind)]


async def test_a_well_done_reaches_the_child_and_is_said(supervisor: Supervisor) -> None:
    await supervisor.start()
    note_id = supervisor.send_note(1, "praise", from_name="Anne", parent_id=1, actor="parent:1")
    shown = _events(supervisor, EventKind.PARENT_NOTE)
    assert shown == [{"note": {"id": note_id, "kind": "praise", "text": "", "from": "Anne"}}]
    said = [e for e in _events(supervisor, EventKind.SPEAK) if e["event"] == "praise"]
    assert said and said[0]["vars"] == {"name": "Elif", "parent": "Anne"}
    assert supervisor.shell_state(1)["notes"][0]["id"] == note_id


async def test_the_audit_log_says_a_note_was_sent_and_never_its_words(
    supervisor: Supervisor, session: Session
) -> None:
    await supervisor.start()
    supervisor.send_note(1, "note", "Gizli bir sürpriz var", from_name="Baba", actor="parent:2")
    rows = session.scalars(select(AuditLog).where(AuditLog.action == "note_sent")).all()
    assert len(rows) == 1
    assert rows[0].actor == "parent:2"
    assert "sürpriz" not in rows[0].detail_json
    said = [e["event"] for e in _events(supervisor, EventKind.SPEAK)]
    assert "praise" not in said, "the launcher reads a note out; the daemon says only well done"


async def test_a_note_seen_on_the_screen_stops_waiting(supervisor: Supervisor) -> None:
    await supervisor.start()
    note_id = supervisor.send_note(1, "note", "Seni seviyorum", from_name="Anne")
    assert supervisor.note_seen(1, note_id) is True
    assert supervisor.shell_state(1)["notes"] == []


async def test_an_empty_note_is_refused(supervisor: Supervisor) -> None:
    await supervisor.start()
    with pytest.raises(ValueError):
        supervisor.send_note(1, "note", "   ", from_name="Anne")
    assert _events(supervisor, EventKind.PARENT_NOTE) == []


# -------------------------------------------------------------------- shell


@pytest.fixture
def shell_daemon() -> FakeShellDaemon:
    daemon = FakeShellDaemon(a_state())
    daemon.seen: list[int] = []  # type: ignore[misc]

    async def note_seen(note_id: int) -> bool:
        daemon.seen.append(note_id)  # type: ignore[attr-defined]
        return True

    daemon.note_seen = note_seen  # type: ignore[method-assign]
    return daemon


@pytest.fixture
def shell(config: Config, shell_daemon: FakeShellDaemon) -> TestClient:
    from biokids_shell.app import create_app

    app = create_app(config)
    app.state.shell.client = shell_daemon  # type: ignore[assignment]
    return TestClient(app, base_url=LAUNCHER)


def test_the_home_screen_carries_the_notes_that_waited(
    shell: TestClient, shell_daemon: FakeShellDaemon
) -> None:
    assert shell_daemon.data is not None
    shell_daemon.data["notes"] = [
        {"id": 7, "kind": "note", "text": "Altıda geliyorum", "from": "Anne"}
    ]
    page = shell.get("/").text
    assert 'id="waiting-notes"' in page
    # Jinja's tojson writes the Turkish letters as JSON escapes.
    assert "Alt\\u0131da geliyorum" in page
    assert 'id="note-card"' in page


def test_no_waiting_notes_no_data(shell: TestClient) -> None:
    assert 'id="waiting-notes"' not in shell.get("/").text


def test_the_screen_says_a_note_was_seen(
    shell: TestClient, shell_daemon: FakeShellDaemon
) -> None:
    assert shell.post("/notes/7/seen", json={}).json() == {"ok": True}
    assert shell_daemon.seen == [7]  # type: ignore[attr-defined]
    forged = shell.post("/notes/8/seen", json={}, headers={"Origin": "https://example.com"})
    assert forged.status_code == 403


def test_the_note_strings_are_the_catalogs(shell: TestClient) -> None:
    page = shell.get("/").text
    assert "{from} sana aferin dedi!" in page
    assert "{from} yazd" in page
