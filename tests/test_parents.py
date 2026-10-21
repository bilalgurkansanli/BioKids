"""D-218 - more than one parent.

What has to hold once a family has two parents: each signs in with their own
PIN, under their own name, and a lockout is theirs alone; adding or removing a
parent needs the family's passphrase and never leaves nobody able to sign in;
the audit log says which parent did what; `biokids-admin reset-pin` resets the
PIN of the parent it was asked about and nobody else's. And with one parent -
every machine that upgrades - nothing about signing in changes at all.
"""

from __future__ import annotations

import argparse
import io
import re
import sys
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from biokids_core import admin, mail, repo, security
from biokids_core.config import Config
from biokids_core.db import AuditLog, Database, PanelSession, Parent
from biokids_core.db.models import utcnow
from biokids_panel import auth
from biokids_panel.app import create_app
from test_panel import PASSPHRASE, PIN, FakeDaemon

ROOT = Path(__file__).resolve().parent.parent
ANNE_CODE = "bulut deniz elma fener gemi halka inci kale"
BABA_PIN = "551234"
BABA_CODE = "kitap kalem lale limon masal nane okul pamuk"


@pytest.fixture
def daemon() -> FakeDaemon:
    return FakeDaemon()


@pytest.fixture
def client(config: Config, db: Database, daemon: FakeDaemon) -> TestClient:
    app = create_app(config)
    app.state.ctx.client = daemon  # type: ignore[assignment]
    return TestClient(app)


def _family(db: Database, *, baba: bool) -> None:
    """Setup finished; Anne made it in the wizard, Baba was added later."""
    from biokids_core.db import Child, Policy

    with db.session() as session:
        family = repo.get_or_create_family(session)
        family.setup_completed_at = utcnow()
        session.add(
            Parent(
                display_name="Anne",
                pin_hash=security.hash_secret(PIN),
                passphrase_hash=security.hash_secret(PASSPHRASE),
                recovery_hash=security.hash_secret(ANNE_CODE),
            )
        )
        if baba:
            session.add(
                Parent(
                    display_name="Baba",
                    pin_hash=security.hash_secret(BABA_PIN),
                    recovery_hash=security.hash_secret(BABA_CODE),
                )
            )
        child = Child(unix_user="elif", display_name="Elif", birth_year=2020)
        session.add(child)
        session.flush()
        session.add(Policy(child_id=child.id))


@pytest.fixture
def one_parent(db: Database) -> None:
    _family(db, baba=False)


@pytest.fixture
def two_parents(db: Database) -> None:
    _family(db, baba=True)


def sign_in(client: TestClient, pin: str, parent: int | None = None) -> Any:
    data = {"pin": pin} if parent is None else {"pin": pin, "parent": str(parent)}
    return client.post("/login", data=data, follow_redirects=False)


def signed_in_as(db: Database) -> list[int]:
    with db.session() as session:
        return [row.parent_id for row in session.query(PanelSession).all()]


def csrf_of(client: TestClient, page: str = "/settings/parents") -> str:
    found = re.search(r'name="csrf_token" value="([^"]+)"', client.get(page).text)
    assert found is not None
    return found.group(1)


def shown_recovery_code(page: str) -> str:
    found = re.search(r'name="recovery" value="([^"]+)"', page)
    assert found is not None
    return found.group(1)


def audit(db: Database, action: str) -> list[AuditLog]:
    with db.session() as session:
        return list(session.query(AuditLog).filter(AuditLog.action == action).all())


# ------------------------------------------------------------- one parent


def test_with_one_parent_the_sign_in_page_is_the_pin_box_as_before(
    client: TestClient, one_parent: None
) -> None:
    page = client.get("/login").text
    assert 'name="pin"' in page
    assert "Kim giriş yapıyor" not in page
    assert 'name="parent"' not in page, "no choice to send when there is no choice"
    assert sign_in(client, PIN).status_code == 303


def test_an_upgrade_with_one_parent_signs_in_exactly_as_before(config: Config) -> None:
    """A database made before D-218 and D-217, migrated: the same parent, the
    same PIN form, the same lockout, the default look - and no name on the page."""
    config.paths.db_path.parent.mkdir(parents=True, exist_ok=True)
    alembic = AlembicConfig(str(ROOT / "alembic.ini"))
    alembic.set_main_option(
        "script_location", str(ROOT / "src" / "biokids_core" / "migrations")
    )
    command.upgrade(alembic, "077ca4545f23")
    engine = create_engine(f"sqlite:///{config.paths.db_path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO family (locale, timezone, created_at, setup_completed_at) "
                "VALUES ('tr', 'Europe/Istanbul', '2026-09-20 10:00:00', '2026-09-20 10:05:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO parent (display_name, pin_hash, passphrase_hash, recovery_hash, "
                "pin_failures, pin_lockout_level, created_at) "
                "VALUES ('', :pin, :phrase, '', 0, 0, '2026-09-20 10:01:00')"
            ),
            {"pin": security.hash_secret(PIN), "phrase": security.hash_secret(PASSPHRASE)},
        )
    engine.dispose()
    command.upgrade(alembic, "head")

    client = TestClient(create_app(config))
    client.app.state.ctx.client = FakeDaemon()  # type: ignore[attr-defined]
    page = client.get("/login").text
    assert 'name="pin"' in page and "Kim giriş yapıyor" not in page
    assert sign_in(client, PIN).status_code == 303
    settings = client.get("/settings").text
    assert 'data-text-size="normal"' in settings and 'data-contrast="normal"' in settings
    assert "Ebeveyn 1" in settings, "an unnamed wizard parent still has a name to show"


def test_the_wizard_still_makes_the_first_parent(client: TestClient, db: Database) -> None:
    client.post("/wizard/1", data={"lang": "tr"}, follow_redirects=False)
    client.post(
        "/wizard/2",
        data={"name": "Anne", "pin": PIN, "pin_again": PIN},
        follow_redirects=False,
    )
    with db.session() as session:
        parents = repo.list_parents(session)
        assert [p.display_name for p in parents] == ["Anne"]
        assert repo.get_parent(session) is parents[0]


# ------------------------------------------------------------ signing in


def test_two_parents_are_asked_who_first(client: TestClient, two_parents: None) -> None:
    page = client.get("/login").text
    assert "Kim giriş yapıyor?" in page
    assert re.search(r'name="parent" value="1">\s*Anne', page)
    assert re.search(r'name="parent" value="2">\s*Baba', page)
    assert 'name="pin"' not in page, "the PIN comes after the name"

    chosen = client.get("/login?parent=2").text
    assert 'name="pin"' in chosen and '<input type="hidden" name="parent" value="2">' in chosen
    assert "Baba" in chosen and "Başka biri misiniz?" in chosen


def test_each_parent_signs_in_with_their_own_pin(
    client: TestClient, two_parents: None, db: Database
) -> None:
    assert sign_in(client, BABA_PIN, parent=2).status_code == 303
    assert signed_in_as(db) == [2]

    other = TestClient(client.app)
    assert sign_in(other, PIN, parent=1).status_code == 303
    assert sorted(signed_in_as(db)) == [1, 2]

    with db.session() as session:
        ok = [(e.actor, e.action) for e in repo.recent_audit(session) if e.action == "login_ok"]
    assert sorted(ok) == [("parent:1", "login_ok"), ("parent:2", "login_ok")]


def test_one_parents_pin_does_not_open_the_other(
    client: TestClient, two_parents: None, db: Database
) -> None:
    refused = sign_in(client, BABA_PIN, parent=1)
    assert refused.status_code == 401
    assert "PIN doğru değil" in refused.text
    assert '<input type="hidden" name="parent" value="1">' in refused.text, (
        "asked again as Anne"
    )
    with db.session() as session:
        anne, baba = repo.list_parents(session)
        assert (anne.pin_failures, baba.pin_failures) == (1, 0)


def test_a_pin_that_names_nobody_is_not_counted_against_anyone(
    client: TestClient, two_parents: None, db: Database
) -> None:
    for parent in (None, 7, "x"):
        data: dict[str, str] = {"pin": PIN}
        if parent is not None:
            data["parent"] = str(parent)
        response = client.post("/login", data=data, follow_redirects=False)
        assert response.status_code == 401
        assert "Kim giriş yapıyor?" in response.text
    with db.session() as session:
        assert [p.pin_failures for p in repo.list_parents(session)] == [0, 0]
    assert not audit(db, "login_failed")
    assert signed_in_as(db) == []


def test_a_lockout_is_that_parents_alone(
    client: TestClient, two_parents: None, db: Database, config: Config
) -> None:
    """A child who guesses at Anne's PIN five times has not locked Baba out."""
    for _ in range(config.panel.pin_attempts):
        sign_in(client, "000001", parent=1)
    locked = sign_in(client, PIN, parent=1)
    assert locked.status_code == 401 and "dakika sonra" in locked.text
    assert "disabled" in client.get("/login?parent=1").text
    assert "disabled" not in client.get("/login?parent=2").text

    assert sign_in(client, BABA_PIN, parent=2).status_code == 303
    with db.session() as session:
        anne, baba = repo.list_parents(session)
        assert anne.pin_locked_until is not None and baba.pin_locked_until is None
    assert {row.actor for row in audit(db, "login_locked")} == {"parent:1"}


def test_the_recovery_code_resets_its_own_parents_pin_only(
    client: TestClient, two_parents: None, db: Database
) -> None:
    response = client.post(
        "/recovery", data={"code": BABA_CODE.upper(), "pin": "461379"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login?recovered=1&parent=2"
    assert sign_in(client, "461379", parent=2).status_code == 303
    with db.session() as session:
        anne = repo.get_active_parent(session, 1)
        assert anne is not None and security.verify_secret(anne.pin_hash, PIN)
    (used,) = audit(db, "recovery_used")
    assert used.target == "parent:2"


# ----------------------------------------------------------------- adding


def test_adding_a_parent_needs_the_passphrase(
    client: TestClient, one_parent: None, db: Database
) -> None:
    sign_in(client, PIN)
    page = client.get("/settings/parents").text
    code = shown_recovery_code(page)
    assert security.is_recovery_code(code)
    form = {
        "name": "Baba",
        "pin": BABA_PIN,
        "pin_again": BABA_PIN,
        "recovery": code,
        "written_down": "on",
        "csrf_token": csrf_of(client),
    }

    refused = client.post("/settings/parents/add", data=form)
    assert refused.status_code == 403
    assert shown_recovery_code(refused.text) == code, "the code they wrote down stays"
    wrong = client.post("/settings/parents/add", data={**form, "passphrase": "not the one"})
    assert wrong.status_code == 403
    assert audit(db, "passphrase_failed")
    with db.session() as session:
        assert len(repo.list_parents(session)) == 1

    added = client.post(
        "/settings/parents/add", data={**form, "passphrase": PASSPHRASE}, follow_redirects=False
    )
    assert added.status_code == 303
    assert added.headers["location"] == "/settings/parents?saved=parent_added"
    with db.session() as session:
        baba = repo.find_parent_by_name(session, " baba ")
        assert baba is not None
        assert security.verify_secret(baba.pin_hash, BABA_PIN)
        assert security.verify_recovery_code(baba.recovery_hash, code)
        assert not baba.passphrase_hash, "the passphrase stays the family's one"
    (row,) = audit(db, "parent_added")
    assert (row.actor, row.target, row.detail["name"]) == ("parent:1", "parent:2", "Baba")

    fresh = TestClient(client.app)
    assert sign_in(fresh, BABA_PIN, parent=2).status_code == 303


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"name": " anne "}, "Bu adda bir ebeveyn zaten var"),
        ({"name": "   "}, "Bir ad yazın"),
        ({"pin_again": "551235"}, "İki PIN aynı değil"),
        ({"pin": "111111", "pin_again": "111111"}, "çok kolay tahmin edilir"),
        ({"written_down": ""}, "kodu yazdığını onaylayın"),
        ({"recovery": ""}, "Kurtarma kodu sayfadan doğru gelmedi"),
    ],
)
def test_adding_says_why_it_did_not(
    client: TestClient, one_parent: None, db: Database, change: dict[str, str], reason: str
) -> None:
    sign_in(client, PIN)
    code = shown_recovery_code(client.get("/settings/parents").text)
    form = {
        "name": "Baba",
        "pin": BABA_PIN,
        "pin_again": BABA_PIN,
        "recovery": code,
        "written_down": "on",
        "passphrase": PASSPHRASE,
        "csrf_token": csrf_of(client),
    }
    response = client.post("/settings/parents/add", data={**form, **change})
    assert response.status_code == 400
    assert reason in response.text
    assert BABA_PIN not in response.text, "a PIN is never put back into the page"
    with db.session() as session:
        assert len(repo.list_parents(session)) == 1


def test_adding_needs_a_signed_in_parent_and_a_real_form(
    client: TestClient, one_parent: None, db: Database
) -> None:
    form = {"name": "Baba", "pin": BABA_PIN, "pin_again": BABA_PIN, "passphrase": PASSPHRASE}
    anonymous = client.post("/settings/parents/add", data=form, follow_redirects=False)
    assert anonymous.headers["location"].startswith("/login")
    sign_in(client, PIN)
    forged = client.post("/settings/parents/add", data=form, follow_redirects=False)
    assert forged.headers["location"] == "/settings/parents?error=csrf"
    with db.session() as session:
        assert len(repo.list_parents(session)) == 1


# --------------------------------------------------------------- removing


def test_removing_a_parent_needs_the_passphrase_and_ends_their_sessions(
    client: TestClient, two_parents: None, db: Database
) -> None:
    baba = TestClient(client.app)
    sign_in(baba, BABA_PIN, parent=2)
    sign_in(client, PIN, parent=1)
    csrf = csrf_of(client)

    refused = client.post(
        "/settings/parents/2/remove", data={"csrf_token": csrf}, follow_redirects=False
    )
    assert refused.headers["location"] == "/settings/parents?error=passphrase"
    with db.session() as session:
        assert len(repo.list_parents(session)) == 2

    removed = client.post(
        "/settings/parents/2/remove",
        data={"csrf_token": csrf, "passphrase": PASSPHRASE},
        follow_redirects=False,
    )
    assert removed.headers["location"] == "/settings/parents?saved=parent_removed"
    assert baba.get("/today", follow_redirects=False).status_code == 303, "signed out"
    assert sign_in(baba, BABA_PIN, parent=2).status_code == 401
    with db.session() as session:
        row = session.get(Parent, 2)
        assert row is not None and row.removed_at is not None
        assert (row.pin_hash, row.recovery_hash) == ("", ""), "no secret is kept"
        assert row.display_name == "Baba", "the log can still name them"
        assert [p.display_name for p in repo.list_parents(session)] == ["Anne"]
    (row_removed,) = audit(db, "parent_removed")
    assert (row_removed.actor, row_removed.target) == ("parent:1", "parent:2")
    assert "Kim giriş yapıyor" not in TestClient(client.app).get("/login").text


def test_the_last_parent_is_never_removed(
    client: TestClient, one_parent: None, db: Database
) -> None:
    sign_in(client, PIN)
    response = client.post(
        "/settings/parents/1/remove",
        data={"csrf_token": csrf_of(client), "passphrase": PASSPHRASE},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/settings/parents?error=parents.remove.last"
    with db.session() as session:
        assert len(repo.list_parents(session)) == 1
    assert client.get("/today").status_code == 200, "still signed in"


def test_removing_yourself_hands_the_family_its_passphrase_and_mail(
    client: TestClient, two_parents: None, db: Database, config: Config
) -> None:
    """Anne ran the wizard, so the family's passphrase and mail are on her row.
    She removes herself; Baba is left with both, and the gate still holds."""
    with db.session() as session:
        mail.save_settings(
            session,
            config,
            mail.MailSettings(host="smtp.example.com", username="a@b.c", recipients=("a@b.c",)),
        )
    sign_in(client, PIN, parent=1)
    response = client.post(
        "/settings/parents/1/remove",
        data={"csrf_token": csrf_of(client), "passphrase": PASSPHRASE},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/login"
    assert client.get("/today", follow_redirects=False).status_code == 303
    with db.session() as session:
        holder = repo.get_parent(session)
        assert holder is not None and holder.display_name == "Baba"
        assert security.verify_secret(repo.family_passphrase_hash(session), PASSPHRASE)
        assert mail.load_settings(session, config) is not None
        assert session.get(Parent, 1).smtp_json_encrypted is None  # type: ignore[union-attr]
    assert sign_in(client, BABA_PIN).status_code == 303, "one parent again: no choice"


def test_a_session_of_a_removed_parent_is_refused_even_if_it_survived(
    db: Database, config: Config, two_parents: None
) -> None:
    with db.session() as session:
        result = auth.attempt_login(session, config, BABA_PIN, parent_id=2)
        assert result.ok
        baba = session.get(Parent, 2)
        assert baba is not None
        baba.removed_at = utcnow()
    with db.session() as session:
        assert not auth.resolve(session, config, result.token).authenticated


# ---------------------------------------------------- what is the family's


def test_the_second_parent_uses_the_familys_passphrase(
    client: TestClient, two_parents: None, db: Database, config: Config
) -> None:
    sign_in(client, BABA_PIN, parent=2)
    page = client.get("/children/1/edit").text
    assert "Parolanızı doğrulayın" in page, "asked for the passphrase, not Baba's PIN"
    with db.session() as session:
        token = client.cookies.get(auth.SESSION_COOKIE)
        state = auth.resolve(session, config, token)
        assert not auth.confirm_passphrase(session, state, BABA_PIN)
        assert auth.confirm_passphrase(session, state, PASSPHRASE)


def test_the_second_parent_cannot_replace_the_passphrase_without_it(
    client: TestClient, two_parents: None, db: Database
) -> None:
    """Read from Baba's own row - which has none - this was "setting the first
    passphrase", and needed no proof at all."""
    sign_in(client, BABA_PIN, parent=2)
    response = client.post(
        "/settings/passphrase",
        data={
            "current_pin": BABA_PIN,
            "current_passphrase": "",
            "new_passphrase": "yeni ve uzun bir parola",
            "csrf_token": csrf_of(client, "/settings"),
        },
        follow_redirects=False,
    )
    assert response.headers["location"] == "/settings?error=passphrase"
    with db.session() as session:
        assert security.verify_secret(repo.family_passphrase_hash(session), PASSPHRASE)
    assert [row.actor for row in audit(db, "passphrase_failed")] == ["parent:2"]

    changed = client.post(
        "/settings/passphrase",
        data={
            "current_pin": BABA_PIN,
            "current_passphrase": PASSPHRASE,
            "new_passphrase": "yeni ve uzun bir parola",
            "csrf_token": csrf_of(client, "/settings"),
        },
        follow_redirects=False,
    )
    assert changed.headers["location"] == "/settings?saved=1"
    with db.session() as session:
        assert security.verify_secret(
            repo.family_passphrase_hash(session), "yeni ve uzun bir parola"
        )
        baba = session.get(Parent, 2)
        assert baba is not None and not baba.passphrase_hash, "still the family's, on Anne"


def test_guessing_the_passphrase_on_the_settings_form_is_limited(
    client: TestClient, two_parents: None, db: Database
) -> None:
    """The settings form was a place to guess with no limit at all (D-171)."""
    sign_in(client, PIN, parent=1)
    csrf = csrf_of(client, "/settings")
    form = {"current_pin": PIN, "new_passphrase": "yeni ve uzun bir parola", "csrf_token": csrf}
    for _ in range(auth.PASSPHRASE_ATTEMPTS):
        client.post("/settings/passphrase", data={**form, "current_passphrase": "yanlis"})
    blocked = client.post(
        "/settings/passphrase",
        data={**form, "current_passphrase": PASSPHRASE},
        follow_redirects=False,
    )
    assert blocked.headers["location"] == "/settings?error=passphrase"
    with db.session() as session:
        assert security.verify_secret(repo.family_passphrase_hash(session), PASSPHRASE)


# ------------------------------------------------------------- the audit log


def test_the_audit_log_names_the_parent_who_did_it(
    client: TestClient, two_parents: None, db: Database, daemon: FakeDaemon
) -> None:
    sign_in(client, BABA_PIN, parent=2)
    client.post(
        "/settings/appearance",
        data={"text_size": "large", "csrf_token": csrf_of(client, "/settings")},
    )
    (row,) = audit(db, "appearance_changed")
    assert row.actor == "parent:2"

    rows = client.get("/audit").text
    rows = rows[rows.index("<tbody>") : rows.index("</tbody>")]
    assert "Baba" in rows
    assert "Panelin görünümü değişti" in rows


def test_a_removed_parent_is_still_named_in_the_log(
    client: TestClient, two_parents: None, db: Database
) -> None:
    baba = TestClient(client.app)
    sign_in(baba, BABA_PIN, parent=2)
    sign_in(client, PIN, parent=1)
    client.post(
        "/settings/parents/2/remove",
        data={"csrf_token": csrf_of(client), "passphrase": PASSPHRASE},
    )
    rows = client.get("/audit?action=login_ok").text
    rows = rows[rows.index("<tbody>") : rows.index("</tbody>")]
    assert "Baba" in rows and "Anne" in rows


def test_the_mail_says_a_parent_was_added(db: Database, two_parents: None) -> None:
    from biokids_core.i18n import translator
    from biokids_daemon import notifier

    assert {"parent_added", "parent_removed"} <= notifier.MAILED_ACTIONS
    with db.session() as session:
        row = repo.audit(session, actor="parent:1", action="parent_added", target="parent:2")
        line = notifier._describe(row, {}, translator("tr"), repo.parent_names(session))
    assert line == "Baba: Ebeveyn eklendi"


def test_a_parent_renames_only_themselves(
    client: TestClient, two_parents: None, db: Database
) -> None:
    sign_in(client, BABA_PIN, parent=2)
    csrf = csrf_of(client)
    taken = client.post(
        "/settings/parents/name",
        data={"name": "ANNE", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert taken.headers["location"] == "/settings/parents?error=parents.name.taken"
    client.post("/settings/parents/name", data={"name": "  Babacım ", "csrf_token": csrf})
    with db.session() as session:
        assert [p.display_name for p in repo.list_parents(session)] == ["Anne", "Babacım"]
    (row,) = audit(db, "parent_renamed")
    assert (row.actor, row.detail["after"]) == ("parent:2", "Babacım")


# ---------------------------------------------------------- the requests


def test_the_requests_say_which_parent_answered(
    client: TestClient, two_parents: None, db: Database
) -> None:
    from biokids_core.db import AccessRequest

    with db.session() as session:
        session.add(
            AccessRequest(
                child_id=1,
                url="example.com",
                kind="site",
                status="denied",
                decided_by=2,
                decided_at=utcnow(),
            )
        )
    sign_in(client, PIN, parent=1)
    page = client.get("/requests").text
    assert "· Baba" in page


# ---------------------------------------------------------------- reset-pin


def _reset(config: Config, pin: str, *, parent: str | None = None) -> int:
    with mock.patch.object(sys, "stdin", io.StringIO(f"{pin}\n")):
        return admin.cmd_reset_pin(argparse.Namespace(stdin=True, parent=parent), config)


def test_reset_pin_asks_whose_when_there_are_two(
    config: Config, db: Database, two_parents: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _reset(config, "461379") == 2
    assert "--parent" in capsys.readouterr().out
    with db.session() as session:
        anne, baba = repo.list_parents(session)
        assert security.verify_secret(anne.pin_hash, PIN)
        assert security.verify_secret(baba.pin_hash, BABA_PIN)


def test_reset_pin_resets_the_named_parent_only(
    config: Config, db: Database, two_parents: None
) -> None:
    with db.session() as session:
        baba = repo.get_active_parent(session, 2)
        assert baba is not None
        baba.pin_failures, baba.pin_lockout_level = 4, 2
        baba.pin_locked_until = utcnow()

    assert _reset(config, "461379", parent=" baba ") == 0

    with db.session() as session:
        anne, baba = repo.list_parents(session)
        assert security.verify_secret(anne.pin_hash, PIN), "Anne's PIN is untouched"
        assert security.verify_secret(baba.pin_hash, "461379")
        assert (baba.pin_failures, baba.pin_lockout_level, baba.pin_locked_until) == (
            0,
            0,
            None,
        )
    (row,) = audit(db, "pin_changed")
    assert (row.actor, row.target) == ("cli", "parent:2")


def test_reset_pin_refuses_a_name_that_is_nobodys(
    config: Config, db: Database, two_parents: None
) -> None:
    assert _reset(config, "461379", parent="Dede") == 2
    assert not audit(db, "pin_changed")


def test_reset_pin_asks_by_name_on_a_terminal(
    config: Config, db: Database, two_parents: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: "Anne")
    monkeypatch.setattr(admin.getpass, "getpass", lambda _prompt: "461379")
    assert admin.cmd_reset_pin(argparse.Namespace(stdin=False, parent=None), config) == 0
    with db.session() as session:
        anne, baba = repo.list_parents(session)
        assert security.verify_secret(anne.pin_hash, "461379")
        assert security.verify_secret(baba.pin_hash, BABA_PIN)


def test_reset_pin_with_one_parent_needs_no_name(
    config: Config, db: Database, one_parent: None
) -> None:
    assert _reset(config, "461379") == 0
    assert _reset(config, "461380", parent="Anne") == 0
    assert _reset(config, "461381", parent="Baba") == 2
    with db.session() as session:
        parent = repo.get_parent(session)
        assert parent is not None and security.verify_secret(parent.pin_hash, "461380")


def test_reset_pin_takes_the_name_but_never_the_pin_as_an_argument() -> None:
    parser = admin.build_parser()
    args = parser.parse_args(["reset-pin", "--stdin", "--parent", "Baba"])
    assert args.parent == "Baba"
    with pytest.raises(SystemExit):
        parser.parse_args(["reset-pin", "--parent", "Baba", "551234"])


# ---------------------------------------------------------- reset-passphrase


def _reset_passphrase(config: Config, passphrase: str) -> int:
    with mock.patch.object(sys, "stdin", io.StringIO(f"{passphrase}\n")):
        return admin.cmd_reset_passphrase(argparse.Namespace(stdin=True), config)


def test_reset_passphrase_sets_the_familys(
    config: Config, db: Database, two_parents: None
) -> None:
    """A forgotten passphrase had no way back (D-220)."""
    assert _reset_passphrase(config, "yeni aile parolası") == 0
    with db.session() as session:
        assert security.verify_secret(
            repo.family_passphrase_hash(session), "yeni aile parolası"
        )
        assert not security.verify_secret(repo.family_passphrase_hash(session), PASSPHRASE)
    (row,) = audit(db, "passphrase_changed")
    assert row.actor == "cli"


def test_reset_passphrase_refuses_a_short_one(
    config: Config, db: Database, one_parent: None
) -> None:
    assert _reset_passphrase(config, "kısa") == 2
    with db.session() as session:
        assert security.verify_secret(repo.family_passphrase_hash(session), PASSPHRASE)
    assert not audit(db, "passphrase_changed")


def test_reset_passphrase_asks_twice_on_a_terminal(
    config: Config, db: Database, one_parent: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = iter(["ilk yazılan parola", "başka bir parola"])
    monkeypatch.setattr(admin.getpass, "getpass", lambda _prompt: next(answers))
    assert admin.cmd_reset_passphrase(argparse.Namespace(stdin=False), config) == 2
    assert not audit(db, "passphrase_changed")


def test_guesses_at_the_old_passphrase_do_not_lock_the_new_one(
    config: Config, db: Database, one_parent: None
) -> None:
    with db.session() as session:
        for _ in range(auth.PASSPHRASE_ATTEMPTS):
            repo.audit(session, actor="parent:1", action="passphrase_failed")
    with db.session() as session:
        assert auth.passphrase_locked(session)
    assert _reset_passphrase(config, "yeni aile parolası") == 0
    with db.session() as session:
        assert not auth.passphrase_locked(session), "the new passphrase works at once"
