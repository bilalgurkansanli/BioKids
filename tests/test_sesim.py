"""D-240: Sesim - the child records a story or a song and hears it back.

The recordings are the child's, in their own home, so most of what matters is
what the launcher will and will not write and serve: only a short mono WAV it
read the header of, only names it made, only the child's own regular files,
never a link. Then the one way out: "Büyüğüme gönder", made small for the
parent's phone, at most a few a day, and heard on the parent's Today card.
"""

from __future__ import annotations

import base64
import datetime as dt
import os
import struct
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

import test_panel
from biokids_core.clock import FakeClock
from biokids_core.config import Config
from biokids_core.db import AuditLog, Child, Database, Policy
from biokids_core.db.models import SharedRecording
from biokids_daemon.bus import EventBus
from biokids_daemon.enforcer import Enforcer, RecordingRunner
from biokids_daemon.operations import SHARED_RECORDINGS_PER_DAY
from biokids_daemon.sessions import FakeSessionSource, SessionWatcher
from biokids_daemon.supervisor import Supervisor
from biokids_shell import recordings
from test_panel import FakeDaemon, login
from test_shell import LAUNCHER, FakeShellDaemon, a_state

client = test_panel.client
daemon = test_panel.daemon
family_ready = test_panel.family_ready

UID = os.getuid() if hasattr(os, "getuid") else None


def wav(seconds: float, rate: int = 16000, *, channels: int = 1, bits: int = 16) -> bytes:
    frames = int(seconds * rate)
    pcm = b"".join(struct.pack("<h", (n * 37) % 2000 - 1000) for n in range(frames))
    block = channels * bits // 8
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    header += b"fmt " + struct.pack(
        "<IHHIIHH", 16, 1, channels, rate, rate * block, block, bits
    )
    return header + b"data" + struct.pack("<I", len(pcm)) + pcm


# ---------------------------------------------------------------- the files


def test_a_recording_is_kept_and_listed(tmp_path: Path) -> None:
    kept = recordings.save(tmp_path, UID, wav(2), now=dt.datetime(2026, 10, 23, 17, 5, 9))
    assert kept.name == "sesim-20261023-170509.wav" and kept.seconds == 2.0
    listed = recordings.recordings(tmp_path, UID)
    assert [r.name for r in listed] == [kept.name]
    assert recordings.read(tmp_path, UID, kept.name) == wav(2)


@pytest.mark.parametrize(
    ("data", "key"),
    [
        (b"not a wav at all, just words", "sesim.error.not_audio"),
        (wav(1, channels=2), "sesim.error.not_audio"),
        (wav(1, bits=8), "sesim.error.not_audio"),
        (wav(1, rate=12345), "sesim.error.not_audio"),
        (wav(0.2), "sesim.error.too_short"),
        (wav(recordings.MAX_SECONDS + 3, rate=8000), "sesim.error.too_long"),
    ],
    ids=["words", "stereo", "8-bit", "odd-rate", "too-short", "too-long"],
)
def test_only_a_short_mono_recording_is_kept(tmp_path: Path, data: bytes, key: str) -> None:
    with pytest.raises(recordings.RecordingError) as raised:
        recordings.save(tmp_path, UID, data)
    assert raised.value.key == key
    assert recordings.recordings(tmp_path, UID) == []


@pytest.mark.parametrize(
    "name",
    ["../secret.wav", "sesim-1.wav", ".sesim-20261023-170509.wav", "x/../y.wav", "a.mp3"],
)
def test_only_names_it_made_are_read(tmp_path: Path, name: str) -> None:
    assert recordings.read(tmp_path, UID, name) is None
    assert recordings.delete(tmp_path, UID, name) is False


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt", reason="symbolic links")
def test_a_link_is_never_a_recording(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere.wav"
    elsewhere.write_bytes(wav(1))
    place = recordings.folder(tmp_path)
    place.mkdir(parents=True)
    (place / "sesim-20261023-170509.wav").symlink_to(elsewhere)
    assert recordings.read(tmp_path, UID, "sesim-20261023-170509.wav") is None
    assert recordings.recordings(tmp_path, UID) == []


def test_a_full_folder_asks_the_child_to_delete_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recordings, "MAX_KEPT", 2)
    start = dt.datetime(2026, 10, 23, 17, 0, 0)
    for n in range(2):
        recordings.save(tmp_path, UID, wav(1), now=start + dt.timedelta(seconds=n))
    with pytest.raises(recordings.RecordingError) as raised:
        recordings.save(tmp_path, UID, wav(1), now=start + dt.timedelta(seconds=5))
    assert raised.value.key == "sesim.error.full"
    assert len(recordings.recordings(tmp_path, UID)) == 2, "nothing dropped quietly"


def test_deleting_one(tmp_path: Path) -> None:
    kept = recordings.save(tmp_path, UID, wav(1))
    assert recordings.delete(tmp_path, UID, kept.name) is True
    assert recordings.recordings(tmp_path, UID) == []


def test_what_the_parent_gets_is_small(tmp_path: Path) -> None:
    longest = wav(recordings.MAX_SECONDS)
    small = recordings.shared(longest)
    rate, seconds = recordings.wav_seconds(small)
    assert rate == recordings.SHARED_RATE
    assert abs(seconds - recordings.MAX_SECONDS) < 0.01
    from biokids_core.ipc import MAX_LINE_BYTES

    assert len(base64.b64encode(small)) + 200 < MAX_LINE_BYTES, "one message to the daemon"


# ------------------------------------------------------------------ daemon


@pytest.fixture
def supervisor(db: Database, clock: FakeClock, config: Config, child: Child) -> Supervisor:
    return Supervisor(
        database=db,
        bus=EventBus(queue_size=64, history_size=64),
        watcher=SessionWatcher(FakeSessionSource()),
        enforcer=Enforcer(config, RecordingRunner()),
        clock=clock,
        config=config,
    )


async def test_a_sent_recording_is_kept_for_the_parent(
    supervisor: Supervisor, session: Session
) -> None:
    await supervisor.start()
    small = recordings.shared(wav(3))
    assert supervisor.share_recording(1, base64.b64encode(small).decode(), 3.0) is True
    row = session.scalars(select(SharedRecording)).one()
    assert row.audio == small and row.seconds == 3.0
    audit = session.scalars(select(AuditLog).where(AuditLog.action == "recording_shared")).one()
    assert audit.target == "child:1"


@pytest.mark.parametrize("audio", ["not base64 !!", base64.b64encode(b"x" * 100).decode()])
async def test_only_a_wav_is_kept(supervisor: Supervisor, session: Session, audio: str) -> None:
    await supervisor.start()
    assert supervisor.share_recording(1, audio, 1.0) is False
    assert session.scalars(select(SharedRecording)).all() == []


async def test_a_few_a_day(supervisor: Supervisor, session: Session) -> None:
    await supervisor.start()
    small = base64.b64encode(recordings.shared(wav(1))).decode()
    for _ in range(SHARED_RECORDINGS_PER_DAY):
        assert supervisor.share_recording(1, small, 1.0)
    assert supervisor.share_recording(1, small, 1.0) is False


# ------------------------------------------------------------------- shell


@pytest.fixture
def shell(config: Config, tmp_path: Path) -> tuple[TestClient, FakeShellDaemon]:
    from biokids_shell.app import create_app

    state = a_state()
    state["tiles"].append(
        {"slug": "sesim", "name": "Sesim", "icon": "mic", "kind": "builtin", "category": "free"}
    )
    fake = FakeShellDaemon(state)
    fake.shared: list[tuple[str, float]] = []  # type: ignore[misc]

    async def share_recording(audio: str, seconds: float) -> bool:
        fake.shared.append((audio, seconds))  # type: ignore[attr-defined]
        return True

    fake.share_recording = share_recording  # type: ignore[method-assign]
    app = create_app(config)
    app.state.shell.client = fake  # type: ignore[assignment]
    app.state.shell.home = tmp_path
    return TestClient(app, base_url=LAUNCHER), fake


def test_the_page_records_keeps_and_plays(shell: tuple[TestClient, FakeShellDaemon]) -> None:
    client_, _fake = shell
    page = client_.get("/sesim").text
    assert "Basılı tut ve konuş" in page and 'id="sesim-record"' in page
    saved = client_.post("/sesim", content=wav(2), headers={"Content-Type": "audio/wav"})
    assert saved.json() == {"ok": True}
    page = client_.get("/sesim").text
    assert "data-play=" in page and "Büyüğüme gönder" in page
    name = page.split('data-play="', 1)[1].split('"', 1)[0]
    played = client_.get(f"/sesim/file/{name}")
    assert played.status_code == 200 and played.headers["content-type"] == "audio/wav"
    assert played.headers["cross-origin-resource-policy"] == "same-origin"
    other_site = client_.get(f"/sesim/file/{name}", headers={"Sec-Fetch-Site": "cross-site"})
    assert other_site.status_code == 403


def test_a_bad_recording_is_told_kindly(shell: tuple[TestClient, FakeShellDaemon]) -> None:
    client_, _fake = shell
    short = client_.post("/sesim", content=wav(0.2), headers={"Content-Type": "audio/wav"})
    assert short.json() == {"ok": False, "message": "Çok kısa oldu. Biraz daha uzun konuş."}
    forged = client_.post(
        "/sesim",
        content=wav(2),
        headers={"Content-Type": "audio/wav", "Origin": "https://example.com"},
    )
    assert forged.status_code == 403


def test_sending_one_to_a_grown_up(shell: tuple[TestClient, FakeShellDaemon]) -> None:
    client_, fake = shell
    client_.post("/sesim", content=wav(2), headers={"Content-Type": "audio/wav"})
    name = client_.get("/sesim").text.split('data-share="', 1)[1].split('"', 1)[0]
    assert client_.post(f"/sesim/{name}/share", json={}).json() == {"ok": True}
    ((audio, seconds),) = fake.shared  # type: ignore[attr-defined]
    rate, _length = recordings.wav_seconds(base64.b64decode(audio))
    assert rate == recordings.SHARED_RATE and seconds == 2.0
    assert client_.post(f"/sesim/{name}/delete", json={}).json() == {"ok": True}
    assert "data-play=" not in client_.get("/sesim").text


def test_no_tile_no_page(config: Config) -> None:
    from biokids_shell.app import create_app

    app = create_app(config)
    app.state.shell.client = FakeShellDaemon(a_state())  # type: ignore[assignment]
    response = TestClient(app, base_url=LAUNCHER).get("/sesim", follow_redirects=False)
    assert response.status_code == 303


# ------------------------------------------------------------------- panel


def test_the_parent_hears_it_on_today(
    client: TestClient, family_ready: None, daemon: FakeDaemon, db: Database
) -> None:
    daemon.status = {
        "children": [
            {
                "child_id": 1,
                "display_name": "Elif",
                "managed": True,
                "online": False,
                "allow": True,
                "remaining_seconds": 600,
                "quota_seconds": 3600,
            }
        ]
    }
    small = recordings.shared(wav(3))
    with db.session() as session:
        session.add(SharedRecording(child_id=1, seconds=3.0, audio=small))
        session.commit()
        rid = session.scalars(select(SharedRecording.id)).one()
    login(client)
    page = client.get("/today").text
    assert "Elif size bir ses kaydı gönderdi (3 sn)" in page and "Yeni" in page
    heard = client.get(f"/children/1/recordings/{rid}.wav")
    assert heard.status_code == 200 and heard.content == small
    assert client.get(f"/children/2/recordings/{rid}.wav").status_code == 404
    with db.session() as session:
        assert session.get(SharedRecording, rid).heard_at is not None  # type: ignore[union-attr]


def test_the_preview_shows_the_childs_screen(
    client: TestClient, family_ready: None, db: Database
) -> None:
    from biokids_core.types import Category
    from helpers import give_app

    with db.session() as session:
        give_app(session, 1, "supertux", Category.GAMES)
        give_app(session, 1, "gcompris", Category.LEARNING)
        policy = session.get(Policy, 1)
        assert policy is not None
        policy.learn_first_min = 15
        session.commit()
    login(client)
    page = client.get("/children/1/preview").text
    assert "canlı göstermez" in page, "a preview, not a view of the screen"
    assert "Supertux" in page and "Gcompris" in page
    assert page.count("preview__tile--waiting") == 1, "the game waits for learning"
    assert "Bitireyim" in page
    assert 'href="/children/1/preview"' in client.get("/children").text


@pytest.mark.parametrize("rate", [22050, 44100, 48000])
def test_any_rate_the_page_records_at_can_be_sent(
    shell: tuple[TestClient, FakeShellDaemon], rate: int
) -> None:
    client_, fake = shell
    client_.post("/sesim", content=wav(1.5, rate=rate), headers={"Content-Type": "audio/wav"})
    name = client_.get("/sesim").text.split('data-share="', 1)[1].split('"', 1)[0]
    assert client_.post(f"/sesim/{name}/share", json={}).json() == {"ok": True}
    ((_audio, seconds),) = fake.shared  # type: ignore[attr-defined]
    assert seconds == 1.5


def test_a_listing_reads_only_the_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kept = recordings.save(tmp_path, UID, wav(3))
    reads: list[int] = []
    real = recordings._read_own

    def counting(path: Path, uid: int | None, *, head: int = 0) -> Any:
        reads.append(head)
        return real(path, uid, head=head)

    monkeypatch.setattr(recordings, "_read_own", counting)
    listed = recordings.recordings(tmp_path, UID)
    assert [r.seconds for r in listed] == [kept.seconds] == [3.0]
    assert reads == [recordings.HEADER_BYTES]


def test_a_childs_voice_is_kept_a_week(session: Session, child: Child, config: Config) -> None:
    from biokids_core import retention
    from biokids_core.db.models import utcnow

    small = recordings.shared(wav(1))
    session.add(SharedRecording(child_id=child.id, seconds=1.0, audio=small))
    old = SharedRecording(child_id=child.id, seconds=1.0, audio=small)
    old.created_at = utcnow() - dt.timedelta(days=8)
    session.add(old)
    session.commit()
    assert retention.prune(session, config)["shared_recording"] == 1
