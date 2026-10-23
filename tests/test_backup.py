"""D-238: Yedekle ve taşı - the family's BioKids in one file, for a new computer.

The seal first: the file opens with the passphrase and nothing else, and a
file changed in any byte does not open at all. Then the contents: what the
old computer knew arrives on the new one - the child, their rules, apps, sites,
English, history and the parent's own voice - and what must not travel, the
secrets, is not in the file. Then the panel's two buttons.
"""

from __future__ import annotations

import base64
import datetime as dt
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

import test_panel
from biokids_core import backup, repo
from biokids_core.config import Config
from biokids_core.db import Child, Database, Policy
from biokids_core.db.models import (
    AuditLog,
    CategoryLimit,
    DayOverride,
    EnglishItem,
    FilterRule,
    LedgerDay,
    UsageTick,
)
from biokids_core.types import Category
from helpers import give_app
from test_panel import FakeDaemon, csrf_of, login

# The panel's fixtures, used here as they are there.
client = test_panel.client
daemon = test_panel.daemon
family_ready = test_panel.family_ready

PASS = "uzun bir aile parolası"
TODAY = dt.date(2026, 10, 23)

# The KDF at full strength costs about a second a try; the seal is the same.
FAST_KDF = {"type": "argon2id", "time_cost": 1, "memory_cost": 8192, "parallelism": 1}


@pytest.fixture(autouse=True)
def quick_kdf(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup, "KDF", FAST_KDF)


# ------------------------------------------------------------------ the seal


def test_a_sealed_file_opens_with_its_passphrase() -> None:
    data = backup.seal({"format": 1, "children": []}, PASS)
    assert data.startswith(backup.MAGIC)
    assert backup.open_sealed(data, PASS) == {"format": 1, "children": []}
    assert backup.open_sealed(data, f"  {PASS}  ")["format"] == 1, "spaces around are forgiven"


def test_the_wrong_passphrase_does_not_open_it() -> None:
    data = backup.seal({"format": 1, "children": []}, PASS)
    with pytest.raises(backup.BackupError) as raised:
        backup.open_sealed(data, "başka bir parola")
    assert raised.value.key == "backup.error.passphrase"


def test_a_file_changed_anywhere_does_not_open() -> None:
    data = bytearray(backup.seal({"format": 1, "children": [{"x": 1}]}, PASS))
    for where in (len(backup.MAGIC) + 5, len(data) - 3):
        changed = bytearray(data)
        changed[where] ^= 0x01
        with pytest.raises(backup.BackupError):
            backup.open_sealed(bytes(changed), PASS)


@pytest.mark.parametrize("junk", [b"", b"not a backup", b"BIOKIDS-BACKUP 1\nnot json\n..."])
def test_something_else_is_not_taken_for_a_backup(junk: bytes) -> None:
    with pytest.raises(backup.BackupError) as raised:
        backup.open_sealed(junk, PASS)
    assert raised.value.key == "backup.error.not_ours"


def test_two_backups_of_the_same_family_differ() -> None:
    """A fresh salt and nonce each time: nothing about the file repeats."""
    contents = {"format": 1, "children": []}
    assert backup.seal(contents, PASS) != backup.seal(contents, PASS)


# --------------------------------------------------------- what travels


def _family_with_history(session: Session, child: Child) -> None:
    policy = session.get(Policy, child.id)
    assert policy is not None
    policy.weekday_quota_min = 75
    policy.finish_up_min = 5
    child.birthday = "04-12"
    child.calm_screen = True
    give_app(session, child.id, "gcompris", Category.LEARNING)
    give_app(session, child.id, "supertux", Category.GAMES, override=Category.FREE)
    session.add_all(
        [
            CategoryLimit(child_id=child.id, category="games", minutes=30),
            FilterRule(child_id=child.id, kind="allow", pattern="okul.example.org"),
            DayOverride(
                child_id=child.id,
                start_day=TODAY + dt.timedelta(days=3),
                end_day=TODAY + dt.timedelta(days=5),
                extra_minutes=30,
                label="Bayram",
            ),
            EnglishItem(
                child_id=child.id,
                item="animals/cat",
                box=3,
                due=TODAY,
                first_day=TODAY - dt.timedelta(days=9),
                last_day=TODAY - dt.timedelta(days=1),
                right=4,
            ),
            LedgerDay(child_id=child.id, day=TODAY - dt.timedelta(days=1), general_s=2400),
            UsageTick(
                child_id=child.id,
                day=TODAY - dt.timedelta(days=1),
                category="learning",
                app_slug="gcompris",
                seconds=1200,
            ),
        ]
    )
    session.commit()


def test_what_the_old_computer_knew_arrives_on_the_new_one(
    session: Session, child: Child, tmp_path: Path, config: Config
) -> None:
    _family_with_history(session, child)
    voices = tmp_path / "voices-old"
    (voices / "tr").mkdir(parents=True)
    (voices / "tr" / "warn_10.wav").write_bytes(b"RIFF....WAVEfmt ")
    (voices / "tr" / "warn_10.txt").write_text("On dakikan kaldı yavrum.", encoding="utf-8")

    contents = backup.export(session, recordings_dir=voices, today=TODAY)
    sealed = backup.seal(contents, PASS)
    secrets = ("pin_hash", "passphrase_hash", "recovery_hash", "smtp")
    assert not any(word in str(contents) for word in secrets), "never a secret"

    new_db = Database(config.with_prefix(tmp_path / "new"))
    new_db.create_all()
    new_voices = tmp_path / "voices-new"
    with new_db.session() as new:

        def create(session_: Session, entry: Any) -> Child:
            made = Child(
                unix_user=entry["unix_user"],
                display_name=entry["child"]["display_name"],
                birth_year=entry["child"]["birth_year"],
            )
            session_.add(made)
            session_.flush()
            session_.add(Policy(child_id=made.id))
            session_.flush()
            return made

        from biokids_core.db.models import App

        for slug in ("gcompris", "supertux"):
            new.add(App(slug=slug, name_tr=slug, name_en=slug, exec_cmd=slug))
        new.commit()

        done = backup.restore(
            new,
            backup.open_sealed(sealed, PASS),
            create_child=create,
            known_apps={"gcompris", "supertux"},
            recordings_dir=new_voices,
        )
        new.commit()
        assert done.children == ["Elif"] and len(done.created) == 1
        assert done.recordings == 2

        moved = repo.get_child_by_user(new, "elif")
        assert moved is not None
        assert moved.birthday == "04-12" and moved.calm_screen is True
        policy = new.get(Policy, moved.id)
        assert policy is not None
        assert (policy.weekday_quota_min, policy.finish_up_min) == (75, 5)
        assert repo.load_category_limits(new, moved.id) == {Category.GAMES: 30}
        found = repo.child_app(new, moved.id, "supertux")
        assert found is not None and repo.child_app_category(*found) is Category.FREE
        assert new.scalar(
            select(FilterRule.pattern).where(FilterRule.child_id == moved.id)
        ) == ("okul.example.org")
        assert new.scalar(select(DayOverride.label)) == "Bayram"
        cat = new.get(EnglishItem, {"child_id": moved.id, "item": "animals/cat"})
        assert cat is not None and (cat.box, cat.right) == (3, 4)
        day = new.get(LedgerDay, {"child_id": moved.id, "day": TODAY - dt.timedelta(days=1)})
        assert day is not None and day.general_s == 2400
        assert new.scalar(select(UsageTick.seconds)) == 1200
    new_db.dispose()
    assert (new_voices / "tr" / "warn_10.txt").read_text(encoding="utf-8") == (
        "On dakikan kaldı yavrum."
    )


def test_a_restore_never_writes_over_what_happened_here(session: Session, child: Child) -> None:
    _family_with_history(session, child)
    contents = backup.export(session, today=TODAY)
    here = session.get(LedgerDay, {"child_id": child.id, "day": TODAY - dt.timedelta(days=1)})
    assert here is not None
    here.general_s = 999
    cat = session.get(EnglishItem, {"child_id": child.id, "item": "animals/cat"})
    assert cat is not None
    cat.box = 5
    session.commit()

    backup.restore(
        session, contents, create_child=lambda *_: None, known_apps={"gcompris", "supertux"}
    )
    session.commit()
    assert here.general_s == 999, "this machine's day stays"
    assert cat.box == 5, "a word known better here stays known better"


def test_an_app_this_catalogue_does_not_know_is_left_out(
    session: Session, child: Child
) -> None:
    _family_with_history(session, child)
    contents = backup.export(session, today=TODAY)
    session.delete(repo.child_app(session, child.id, "supertux")[0])  # type: ignore[index]
    session.commit()
    backup.restore(session, contents, create_child=lambda *_: None, known_apps={"gcompris"})
    session.commit()
    assert repo.child_app(session, child.id, "supertux") is None


@pytest.mark.parametrize(
    "name",
    [
        "../etc/passwd",
        "tr/../../x.wav",
        "tr/.hidden.wav",
        "tr/x.sh",
        "a b/x.wav",
        "tr/sub/x.wav",
    ],
)
def test_a_recording_cannot_be_written_outside_its_place(tmp_path: Path, name: str) -> None:
    written = backup._restore_recordings(
        {name: base64.b64encode(b"x").decode()}, tmp_path / "voices"
    )
    assert written == 0
    assert not any(tmp_path.rglob("x*"))


# ------------------------------------------------------------------ the panel


@pytest.fixture
def with_passphrase(db: Database, family_ready: None) -> None:
    from biokids_core import security

    with db.session() as session:
        parent = repo.get_parent(session)
        assert parent is not None
        parent.passphrase_hash = security.hash_secret(PASS)
        session.commit()


def test_a_backup_needs_the_passphrase(
    client: TestClient, with_passphrase: None, db: Database
) -> None:
    login(client)
    assert "Yedeği indir" in client.get("/settings/backup").text
    wrong = client.post(
        "/settings/backup", data={"passphrase": "yanlış parola", "csrf_token": csrf_of(client)}
    )
    assert wrong.status_code == 403
    good = client.post(
        "/settings/backup", data={"passphrase": PASS, "csrf_token": csrf_of(client)}
    )
    assert good.status_code == 200
    assert "attachment" in good.headers["content-disposition"]
    assert good.headers["content-disposition"].endswith('.biokids"')
    contents = backup.open_sealed(good.content, PASS)
    assert [c["unix_user"] for c in contents["children"]] == ["elif"]
    with db.session() as session:
        assert backup.last_taken(session) is not None
        actions = [row.action for row in session.scalars(select(AuditLog))]
    assert "backup_taken" in actions and "passphrase_failed" in actions


def test_no_passphrase_no_backup(client: TestClient, family_ready: None, db: Database) -> None:
    with db.session() as session:
        parent = repo.get_parent(session)
        assert parent is not None
        parent.passphrase_hash = ""
        session.commit()
    login(client)
    page = client.get("/settings/backup").text
    assert "önce bir aile parolası belirleyin" in page.lower()


def test_a_backup_comes_back_through_the_panel(
    client: TestClient, with_passphrase: None, db: Database, daemon: FakeDaemon
) -> None:
    login(client)
    data = client.post(
        "/settings/backup", data={"passphrase": PASS, "csrf_token": csrf_of(client)}
    ).content
    with db.session() as session:
        child = repo.get_child_by_user(session, "elif")
        assert child is not None
        child.calm_screen = False
        session.commit()

    not_this = client.post(
        "/settings/backup/restore",
        data={"passphrase": "başka bir parola", "csrf_token": csrf_of(client)},
        files={"file": ("yedek.biokids", data, "application/octet-stream")},
    )
    assert not_this.status_code == 403, "a restore is behind this machine's passphrase"
    assert "Bu bilgisayarın aile parolası yanlış" in not_this.text

    wrong = client.post(
        "/settings/backup/restore",
        data={
            "passphrase": PASS,
            "old_passphrase": "eski ama yanlış",
            "csrf_token": csrf_of(client),
        },
        files={"file": ("yedek.biokids", data, "application/octet-stream")},
    )
    assert wrong.status_code == 400 and "Parola yanlış" in wrong.text

    done = client.post(
        "/settings/backup/restore",
        data={"passphrase": PASS, "csrf_token": csrf_of(client)},
        files={"file": ("yedek.biokids", data, "application/octet-stream")},
    )
    assert done.status_code == 200
    assert "Yedek geri yüklendi" in done.text and "Elif" in done.text
    assert daemon.ran("reload")
    with db.session() as session:
        actions = [row.action for row in session.scalars(select(AuditLog))]
    assert "backup_restored" in actions and "backup_restore_failed" in actions


# ------------------------------------------------- what a file may not do


def test_a_file_cannot_switch_protection_off(session: Session, child: Child) -> None:
    """D-48: turning protection off is behind this machine's passphrase, and a
    file sealed with any passphrase at all is not that."""
    contents = backup.export(session, today=TODAY)
    entry = contents["children"][0]
    entry["child"]["enabled"] = False
    entry["policy"]["enabled"] = False
    entry["policy"]["weekday_quota_min"] = 99999
    entry["policy"]["end_action"] = "rm -rf /"
    entry["policy"]["windows_json"] = "[not, an, object]"
    entry["child"]["supervision_indicator"] = "covert"
    entry["child"]["locale"] = "xx"
    entry["child"]["birthday"] = "02-31"
    entry["child"]["avatar"] = "../../etc/passwd"
    entry["category_limits"] = {"games": -5}
    before = session.get(Policy, child.id)
    assert before is not None
    quota, action, windows = before.weekday_quota_min, before.end_action, before.windows_json

    backup.restore(
        session,
        contents,
        create_child=lambda *_: None,
        known_apps=set(),
        known_avatars={"fox", "owl"},
    )
    session.commit()
    assert child.enabled is True and before.enabled is True, "never switched off by a file"
    assert (before.weekday_quota_min, before.end_action, before.windows_json) == (
        quota,
        action,
        windows,
    ), "values a form would refuse are left as they were"
    assert child.supervision_indicator == "badge" and child.locale == "tr"
    assert child.birthday is None and child.avatar == "fox"
    assert repo.load_category_limits(session, child.id) == {}


def test_a_file_can_switch_protection_on(session: Session, child: Child) -> None:
    policy = session.get(Policy, child.id)
    assert policy is not None
    contents = backup.export(session, today=TODAY)
    policy.enabled = False
    session.commit()
    backup.restore(session, contents, create_child=lambda *_: None, known_apps=set())
    session.commit()
    assert policy.enabled is True


def test_a_file_does_not_choose_how_hard_the_panel_works() -> None:
    """The header is not authenticated until the key is made: a file naming its
    own argon2 cost could hold the panel for as long as it liked."""
    data = backup.seal({"format": 1, "children": []}, PASS)
    header_end = data.index(b"\n", len(backup.MAGIC))
    header = data[len(backup.MAGIC) : header_end].replace(
        b'"time_cost": 1', b'"time_cost": 99999'
    )
    forged = data[: len(backup.MAGIC)] + header + data[header_end:]
    assert forged != data
    with pytest.raises(backup.BackupError) as raised:
        backup.open_sealed(forged, PASS)
    assert raised.value.key == "backup.error.not_ours"
