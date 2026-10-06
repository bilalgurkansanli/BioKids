"""T4 — the parent panel.

The tests that matter here are about the gate, not about the markup: that the
wizard cannot be re-run once a family exists, that five wrong PINs lock the door
and the sixth attempt is refused even after a restart, that a destructive action
needs a CSRF token, and that every page still renders when the daemon is not
answering — because a panel that breaks when the daemon dies is a panel that
hides the one thing a parent needs to know.

The daemon is faked at the client boundary, so none of this needs a socket.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from biokids_core import repo, security
from biokids_core.config import Config
from biokids_core.db import AuditLog, Database, Parent
from biokids_core.db.models import utcnow
from biokids_core.errors import IpcError
from biokids_core.ipc import Response
from biokids_panel import auth, wizard
from biokids_panel.app import create_app

PIN = "284913"
PASSPHRASE = "acik kapi mavi deniz"


class FakeDaemon:
    """Stands in for the daemon socket. Records what the panel asked for."""

    def __init__(self, *, up: bool = True) -> None:
        self.up = up
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.status: dict[str, Any] = {"children": [], "ticks": 0}

    def call(self, op: str, **args: Any) -> Response:
        if not self.up:
            raise IpcError("the daemon is not running")
        self.calls.append((str(op), args))
        if str(op) == "get_status":
            return Response(ok=True, data=self.status)
        return Response(ok=True, data={})

    def available(self) -> bool:
        return self.up

    def ran(self, op: str) -> bool:
        return any(name == op for name, _ in self.calls)


@pytest.fixture
def daemon() -> FakeDaemon:
    return FakeDaemon()


@pytest.fixture
def client(config: Config, db: Database, daemon: FakeDaemon) -> TestClient:
    app = create_app(config)
    app.state.ctx.client = daemon  # type: ignore[assignment]
    return TestClient(app)


@pytest.fixture
def family_ready(db: Database, config: Config) -> None:
    """A finished setup: one parent with a PIN, one child, one policy."""
    from biokids_core.db import Child, Policy

    with db.session() as session:
        family = repo.get_or_create_family(session)
        family.setup_completed_at = utcnow()
        session.add(
            Parent(
                display_name="Anne",
                pin_hash=security.hash_secret(PIN),
                passphrase_hash=security.hash_secret(PASSPHRASE),
                recovery_hash=security.hash_secret(
                    "bulut deniz elma fener gemi halka inci kale"
                ),
            )
        )
        child = Child(unix_user="elif", display_name="Elif", birth_year=2020)
        session.add(child)
        session.flush()
        session.add(Policy(child_id=child.id))


def login(client: TestClient, pin: str = PIN) -> None:
    response = client.post("/login", data={"pin": pin}, follow_redirects=False)
    assert response.status_code == 303, response.text
    assert auth.SESSION_COOKIE in response.cookies


def _audit_rows(page: str) -> str:
    """Just the table body, so a filter assertion is not fooled by the dropdown."""
    start = page.index("<tbody>")
    return page[start : page.index("</tbody>", start)]


def csrf_of(client: TestClient) -> str:
    """Pull the token out of a rendered page, the way a browser would."""
    page = client.get("/today").text
    marker = 'name="csrf_token" value="'
    start = page.index(marker) + len(marker)
    return page[start : page.index('"', start)]


# -------------------------------------------------------------------- setup


def test_a_fresh_install_goes_straight_to_the_wizard(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/wizard/1"


def test_every_page_redirects_to_the_wizard_until_setup_is_done(client: TestClient) -> None:
    for path in ("/today", "/children", "/audit", "/settings", "/login"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/wizard/1", path


def test_the_wizard_refuses_to_run_twice(client: TestClient, family_ready: None) -> None:
    response = client.get("/wizard/1", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_the_whole_wizard_can_be_completed(
    client: TestClient, db: Database, daemon: FakeDaemon
) -> None:
    assert (
        client.post("/wizard/1", data={"lang": "tr"}, follow_redirects=False).status_code == 303
    )

    page = client.get("/wizard/2").text
    assert "PIN" in page
    assert (
        client.post(
            "/wizard/2",
            data={"name": "Anne", "pin": PIN, "pin_again": PIN},
            follow_redirects=False,
        ).status_code
        == 303
    )

    # The recovery code is rendered once and posted back; only its hash is kept.
    page = client.get("/wizard/3").text
    marker = 'name="recovery" value="'
    start = page.index(marker) + len(marker)
    recovery = page[start : page.index('"', start)]
    assert len(recovery.split()) == 8

    assert (
        client.post(
            "/wizard/3",
            data={"passphrase": PASSPHRASE, "recovery": recovery, "written_down": "on"},
            follow_redirects=False,
        ).status_code
        == 303
    )

    this_year = dt.date.today().year
    assert (
        client.post(
            "/wizard/4",
            data={
                "display_name": "Elif",
                "birth_year": this_year - 6,
                "unix_user": "elif",
                "avatar": "fox",
                "locale": "tr",
            },
            follow_redirects=False,
        ).status_code
        == 303
    )

    client.post("/wizard/5", data={"apps": ["tuxpaint", "gcompris"]}, follow_redirects=False)
    client.post(
        "/wizard/6",
        data={"weekday_quota_min": 45, "weekend_quota_min": 90, "window": "16:00-20:00"},
        follow_redirects=False,
    )
    client.post("/wizard/7", data={"mode": "allowlist"}, follow_redirects=False)
    client.post("/wizard/8", data={"enable": ""}, follow_redirects=False)
    client.post("/wizard/9", follow_redirects=False)
    done = client.post("/wizard/10", data={"voices": "on"}, follow_redirects=False)
    assert done.status_code == 303

    # The closing screen, as the VM showed it: "Adım 11 / 10", and a two-minute
    # trial nothing offered. What a parent needs instead is the restart, because
    # LightDM reads the login-by-picture setting only when it starts (D-155).
    closing = client.get(done.headers["location"]).text
    assert "yeniden başlatın" in closing
    assert "Adım 11" not in closing
    assert "deneme" not in closing

    with db.session() as session:
        assert repo.setup_complete(session)
        child = repo.get_child_by_user(session, "elif")
        assert child is not None
        policy = repo.get_or_create_policy(session, child.id)
        assert policy.weekday_quota_min == 45
        assert policy.windows["weekday"] == ["16:00-20:00"]
        assert {
            slug for _, app in repo.list_child_apps(session, child.id) for slug in [app.slug]
        } == {
            "tuxpaint",
            "gcompris",
        }

    assert daemon.ran("reload"), "the daemon is told to pick up the new policy"


def test_the_wizard_refuses_a_guessable_pin(client: TestClient) -> None:
    client.post("/wizard/1", data={"lang": "tr"})
    response = client.post(
        "/wizard/2", data={"name": "Anne", "pin": "111111", "pin_again": "111111"}
    )
    assert response.status_code == 200
    assert "wizard.parent.pin.weak" not in response.text, "the key must be translated"
    assert "tahmin" in response.text or "guess" in response.text


def test_the_wizard_refuses_two_different_pins(client: TestClient) -> None:
    client.post("/wizard/1", data={"lang": "tr"})
    response = client.post(
        "/wizard/2", data={"name": "Anne", "pin": "284913", "pin_again": "284914"}
    )
    assert response.status_code == 200
    assert "aynı değil" in response.text


def test_the_recovery_code_must_be_confirmed_written_down(client: TestClient) -> None:
    client.post("/wizard/1", data={"lang": "tr"})
    client.post("/wizard/2", data={"name": "Anne", "pin": PIN, "pin_again": PIN})
    response = client.post(
        "/wizard/3", data={"passphrase": PASSPHRASE, "recovery": "a b c d e f g h"}
    )
    assert response.status_code == 200
    assert "yazdığınızı onaylayın" in response.text


# --------------------------------------------------------------------- auth


def test_the_right_pin_signs_in(client: TestClient, family_ready: None) -> None:
    login(client)
    assert client.get("/today").status_code == 200


def test_five_wrong_pins_lock_the_door(
    client: TestClient, family_ready: None, db: Database, config: Config
) -> None:
    for _ in range(config.panel.pin_attempts):
        assert client.post("/login", data={"pin": "000001"}).status_code == 401

    locked = client.post("/login", data={"pin": PIN})
    assert locked.status_code == 401
    assert "dakika sonra" in locked.text

    with db.session() as session:
        parent = repo.get_parent(session)
        assert parent is not None
        assert parent.pin_locked_until is not None


def test_a_lockout_survives_a_panel_restart(
    config: Config, db: Database, family_ready: None, daemon: FakeDaemon
) -> None:
    """A child who pulls the power must not be able to clear the counter."""
    first = TestClient(create_app(config))
    first.app.state.ctx.client = daemon  # type: ignore[attr-defined]
    for _ in range(config.panel.pin_attempts):
        first.post("/login", data={"pin": "000001"})

    second = TestClient(create_app(config))
    second.app.state.ctx.client = daemon  # type: ignore[attr-defined]
    response = second.post("/login", data={"pin": PIN})
    assert response.status_code == 401
    assert "dakika sonra" in response.text


def test_every_pin_attempt_lands_in_the_audit_log(
    client: TestClient, family_ready: None, db: Database
) -> None:
    client.post("/login", data={"pin": "000001"})
    login(client)
    with db.session() as session:
        actions = [entry.action for entry in repo.recent_audit(session, limit=20)]
    assert "login_failed" in actions
    assert "login_ok" in actions


def test_an_idle_session_expires(
    client: TestClient, family_ready: None, db: Database, config: Config
) -> None:
    login(client)
    with db.session() as session:
        from biokids_core.db import PanelSession

        row = session.query(PanelSession).one()
        row.last_seen_at = utcnow() - dt.timedelta(
            minutes=config.panel.session_idle_minutes + 1
        )

    response = client.get("/today", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_signing_out_invalidates_the_session(client: TestClient, family_ready: None) -> None:
    login(client)
    client.post("/logout", data={"csrf_token": csrf_of(client)}, follow_redirects=False)
    assert client.get("/today", follow_redirects=False).status_code == 303


def test_another_site_cannot_sign_a_parent_out(client: TestClient, family_ready: None) -> None:
    login(client)
    client.post("/logout", follow_redirects=False)
    assert client.get("/today", follow_redirects=False).status_code == 200


def test_an_empty_recovery_code_never_resets_the_pin(
    client: TestClient, family_ready: None, db: Database
) -> None:
    """The wizard once hashed whatever its hidden field sent, "" included."""
    with db.session() as session:
        parent = repo.get_parent(session)
        assert parent is not None
        parent.recovery_hash = security.hash_secret("")
    response = client.post("/recovery", data={"code": "", "pin": "135790"})
    assert response.status_code == 400


def test_the_passphrase_gate_stops_after_five_wrong_guesses(
    client: TestClient, family_ready: None, db: Database
) -> None:
    from biokids_panel import auth

    with db.session() as session:
        for _ in range(auth.PASSPHRASE_ATTEMPTS):
            repo.audit(session, actor="parent:1", action="passphrase_failed")
        session.flush()
        assert auth.passphrase_locked(session)


def test_the_recovery_code_resets_the_pin(
    client: TestClient, family_ready: None, db: Database
) -> None:
    response = client.post(
        "/recovery",
        data={"code": "Bulut Deniz Elma Fener Gemi Halka Inci Kale", "pin": "551234"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    login(client, "551234")
    with db.session() as session:
        actions = [e.action for e in repo.recent_audit(session, limit=10)]
    assert "recovery_used" in actions


def test_a_wrong_recovery_code_changes_nothing(client: TestClient, family_ready: None) -> None:
    response = client.post("/recovery", data={"code": "not the code", "pin": "551234"})
    assert response.status_code == 400
    assert client.post("/login", data={"pin": "551234"}).status_code == 401


# ------------------------------------------------------------ parent actions


def test_granting_time_reaches_the_daemon(
    client: TestClient, family_ready: None, daemon: FakeDaemon
) -> None:
    login(client)
    token = csrf_of(client)
    response = client.post(
        "/children/1/grant",
        data={"minutes": 10, "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    _, args = next(c for c in daemon.calls if c[0] == "grant_time")
    assert args["seconds"] == 600
    assert args["child_id"] == 1


def test_an_action_without_a_csrf_token_is_refused(
    client: TestClient, family_ready: None, daemon: FakeDaemon
) -> None:
    login(client)
    response = client.post("/children/1/lock", data={}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].endswith("error=csrf")
    assert not daemon.ran("lock_now")


def test_a_wild_grant_value_is_clamped(
    client: TestClient, family_ready: None, daemon: FakeDaemon
) -> None:
    login(client)
    client.post(
        "/children/1/grant",
        data={"minutes": 999999, "csrf_token": csrf_of(client)},
        follow_redirects=False,
    )
    _, args = next(c for c in daemon.calls if c[0] == "grant_time")
    assert args["seconds"] == 4 * 3600


def test_shutting_down_requires_an_explicit_confirmation(
    client: TestClient, family_ready: None, daemon: FakeDaemon
) -> None:
    login(client)
    token = csrf_of(client)
    client.post(
        "/children/1/poweroff",
        data={"csrf_token": token, "confirm": "no"},
        follow_redirects=False,
    )
    assert not daemon.ran("poweroff_now")

    client.post(
        "/children/1/poweroff",
        data={"csrf_token": token, "confirm": "yes"},
        follow_redirects=False,
    )
    assert daemon.ran("poweroff_now")


# ------------------------------------------------------------ time settings


def test_saving_time_rules_writes_the_policy_and_reloads_the_daemon(
    client: TestClient, family_ready: None, db: Database, daemon: FakeDaemon
) -> None:
    login(client)
    response = client.post(
        "/children/1/time",
        data={
            "weekday_quota_min": 45,
            "weekend_quota_min": 120,
            "weekday_windows": "16:00-20:00, 08:00-09:00",
            "weekend_windows": "",
            "end_action": "lock",
            "enabled": "on",
            "csrf_token": csrf_of(client),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    with db.session() as session:
        policy = repo.get_or_create_policy(session, 1)
        assert policy.weekday_quota_min == 45
        assert policy.windows["weekday"] == ["16:00-20:00", "08:00-09:00"]
        assert policy.end_action == "lock"
        actions = [e.action for e in repo.recent_audit(session, limit=10)]
    assert "policy_changed" in actions
    assert daemon.ran("reload")


def test_a_malformed_window_is_rejected_with_a_readable_message(
    client: TestClient, family_ready: None, db: Database
) -> None:
    login(client)
    response = client.post(
        "/children/1/time",
        data={
            "weekday_quota_min": 45,
            "weekday_windows": "dört-sekiz",
            "end_action": "lock",
            "csrf_token": csrf_of(client),
        },
    )
    assert response.status_code == 400
    assert "16:00-20:00" in response.text
    with db.session() as session:
        assert repo.get_or_create_policy(session, 1).weekday_quota_min == 60


def test_a_quota_cannot_exceed_a_day(
    client: TestClient, family_ready: None, db: Database
) -> None:
    login(client)
    client.post(
        "/children/1/time",
        data={
            "weekday_quota_min": 99999,
            "end_action": "poweroff",
            "enabled": "on",
            "csrf_token": csrf_of(client),
        },
        follow_redirects=False,
    )
    with db.session() as session:
        assert repo.get_or_create_policy(session, 1).weekday_quota_min == 24 * 60


# ------------------------------------------------------- daemon unreachable


def test_the_today_screen_says_so_when_the_daemon_is_down(
    config: Config, db: Database, family_ready: None
) -> None:
    """The one thing a parent must never be left guessing about."""
    app = create_app(config)
    app.state.ctx.client = FakeDaemon(up=False)  # type: ignore[assignment]
    client = TestClient(app)
    login(client)

    page = client.get("/today")
    assert page.status_code == 200
    assert "yanıt vermiyor" in page.text


def test_the_children_are_still_listed_when_the_daemon_is_down(
    config: Config, db: Database, family_ready: None
) -> None:
    """ "Not responding" must not be shown as "no children added yet".

    The children are in a database the panel reads perfectly well; it is only
    their live state that is unknown, and saying so is very different from
    implying a parent never set the product up.
    """
    app = create_app(config)
    app.state.ctx.client = FakeDaemon(up=False)  # type: ignore[assignment]
    client = TestClient(app)
    login(client)

    page = client.get("/today").text
    assert "Elif" in page
    assert "durum bilinmiyor" in page
    assert "Henüz çocuk eklenmemiş" not in page
    assert "+10 dk" not in page, "no action button can work with the daemon gone"


def test_a_second_child_can_be_added_outside_the_wizard(
    client: TestClient, family_ready: None, db: Database, daemon: FakeDaemon
) -> None:
    login(client)
    response = client.post(
        "/children",
        data={
            "display_name": "Kerem",
            "birth_year": dt.date.today().year - 9,
            "unix_user": "kerem",
            "avatar": "owl",
            "locale": "tr",
            "csrf_token": csrf_of(client),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    with db.session() as session:
        kerem = repo.get_child_by_user(session, "kerem")
        assert kerem is not None
        assert kerem.age_band == "8-10"
        policy = repo.get_or_create_policy(session, kerem.id)
        assert policy.weekday_quota_min == 120, "the 8-10 default, not the 4-7 one"
        # On the VM a second child got no tiles at all - not even shutdown (D-156).
        tiles = {app.slug for _link, app in repo.list_child_apps(session, kerem.id)}
        assert {"browser", "volume", "shutdown"} <= tiles
    assert daemon.ran("reload")


def test_skipping_the_app_step_still_leaves_tiles(
    client: TestClient, db: Database, daemon: FakeDaemon
) -> None:
    """ "Şimdilik geç" on step 5 is a link to step 6, so the defaults come first (D-49)."""
    client.post("/wizard/1", data={"lang": "tr"}, follow_redirects=False)
    client.post(
        "/wizard/2", data={"name": "Anne", "pin": PIN, "pin_again": PIN}, follow_redirects=False
    )
    client.post(
        "/wizard/4",
        data={
            "display_name": "Elif",
            "birth_year": dt.date.today().year - 6,
            "unix_user": "elif",
            "avatar": "fox",
            "locale": "tr",
        },
        follow_redirects=False,
    )

    with db.session() as session:
        elif_ = repo.get_child_by_user(session, "elif")
        assert elif_ is not None
        tiles = {app.slug for _link, app in repo.list_child_apps(session, elif_.id)}
    assert {"browser", "volume", "shutdown"} <= tiles


def test_adding_a_child_with_a_bad_username_is_refused(
    client: TestClient, family_ready: None, db: Database
) -> None:
    login(client)
    response = client.post(
        "/children",
        data={
            "display_name": "Kerem",
            "birth_year": dt.date.today().year - 9,
            "unix_user": "Kerem Bey!",
            "csrf_token": csrf_of(client),
        },
    )
    assert response.status_code == 400
    with db.session() as session:
        assert len(repo.list_children(session)) == 1


def test_settings_still_renders_without_the_daemon(
    config: Config, db: Database, family_ready: None
) -> None:
    app = create_app(config)
    app.state.ctx.client = FakeDaemon(up=False)  # type: ignore[assignment]
    client = TestClient(app)
    login(client)
    assert client.get("/settings").status_code == 200


# ------------------------------------------------------------------- pages


def test_the_today_screen_shows_a_childs_remaining_time(
    client: TestClient, family_ready: None, daemon: FakeDaemon
) -> None:
    daemon.status = {
        "children": [
            {
                "child_id": 1,
                "display_name": "Elif",
                "avatar": "fox",
                "managed": True,
                "online": True,
                "app": "TuxPaint",
                "remaining_seconds": 42 * 60,
                "remaining_minutes": 42,
                "quota_seconds": 60 * 60,
                "granted_seconds": 0,
                "used_seconds": 18 * 60,
                "allow": True,
                "reason_code": "allowed",
                "end_action": "poweroff",
            }
        ]
    }
    login(client)
    page = client.get("/today").text
    assert "Elif" in page
    assert "42" in page
    assert "TuxPaint" in page


def test_the_today_card_carries_what_live_js_reads(
    client: TestClient, family_ready: None, daemon: FakeDaemon
) -> None:
    """live.js reads three attributes off the card; the template must write them.

    Each absence had its own visible consequence, and none of them raised
    anything: without `data-label-remaining` the updater fell back to a bare
    "{minutes}" and the tick after page load replaced "42 dk kaldi" with "42";
    without `data-quota` the ring returned early and stayed frozen while the
    number under it counted down.

    Asserted as a contract rather than through a browser, because the failure
    mode is a missing attribute, not broken JavaScript.
    """
    daemon.status = {
        "children": [
            {
                "child_id": 1,
                "display_name": "Elif",
                "avatar": "fox",
                "managed": True,
                "online": True,
                "app": "TuxPaint",
                "remaining_seconds": 42 * 60,
                "remaining_minutes": 42,
                "quota_seconds": 60 * 60,
                "granted_seconds": 0,
                "used_seconds": 18 * 60,
                "allow": True,
                "reason_code": "allowed",
                "end_action": "poweroff",
            }
        ]
    }
    login(client)
    page = client.get("/today").text

    assert 'data-quota="3600"' in page, "the ring cannot move without the quota"
    assert "data-label-remaining=" in page
    assert "dk kald" in page, "the live label has to carry the Turkish, not just a number"
    assert "{minutes}" in page, "live.js substitutes into the translated sentence"
    assert "data-label-time-up=" in page


def test_an_unmanaged_child_is_not_given_a_countdown_by_the_live_updater(
    client: TestClient, family_ready: None, daemon: FakeDaemon
) -> None:
    """No label means the server owns the line.

    A child with no policy shows "no rules are set for this user". Handing
    live.js a label would let the next tick overwrite that with minutes
    remaining - inventing supervision that is not happening, on the one screen
    a parent trusts to tell them whether it is.
    """
    daemon.status = {
        "children": [
            {
                "child_id": 1,
                "display_name": "Elif",
                "avatar": "fox",
                "managed": False,
                "online": True,
                "app": "",
                "remaining_seconds": 0,
                "remaining_minutes": 0,
                "quota_seconds": 0,
                "granted_seconds": 0,
                "used_seconds": 0,
                "allow": True,
                "reason_code": "allowed",
                "end_action": "poweroff",
            }
        ]
    }
    login(client)
    page = client.get("/today").text

    assert "kural tan" in page, "the parent is told there are no rules"
    assert "data-label-remaining=" not in page, "live.js would overwrite that sentence"


def test_the_audit_view_renders_and_filters(
    client: TestClient, family_ready: None, db: Database
) -> None:
    with db.session() as session:
        repo.audit(session, actor="daemon", action="quota_exhausted", target="child:1")
        repo.audit(session, actor="parent:1", action="time_granted", target="child:1")
    login(client)

    assert "Günlük süre doldu" in _audit_rows(client.get("/audit").text)

    # The filter dropdown lists every action that exists, so only the table body
    # can say what the filter actually did.
    rows = _audit_rows(client.get("/audit?action=time_granted").text)
    assert "Süre eklendi" in rows
    assert "Günlük süre doldu" not in rows


def test_the_manifest_is_a_valid_pwa_manifest(client: TestClient, family_ready: None) -> None:
    response = client.get("/manifest.webmanifest")
    assert response.status_code == 200
    body = response.json()
    assert body["display"] == "standalone"
    assert any(icon["purpose"] == "any maskable" for icon in body["icons"])


def test_the_service_worker_is_served_from_the_root(client: TestClient) -> None:
    """At /static/sw.js it could never control a navigation to /today."""
    response = client.get("/sw.js")
    assert response.status_code == 200
    assert response.headers["service-worker-allowed"] == "/"


def test_english_renders_when_the_family_chose_english(
    client: TestClient, family_ready: None, db: Database
) -> None:
    with db.session() as session:
        family = repo.get_family(session)
        assert family is not None
        family.locale = "en"
    login(client)
    assert "Today" in client.get("/today").text


# --------------------------------------------------------------- bind guard


def test_the_panel_refuses_to_bind_to_a_reachable_address() -> None:
    """D-33 in one function: the moment 0.0.0.0 works, someone forwards a port."""
    from biokids_panel.__main__ import check_loopback

    assert check_loopback("127.0.0.1") == "127.0.0.1"
    assert check_loopback("localhost") == "localhost"
    for host in ("0.0.0.0", "192.168.1.5", "::"):
        with pytest.raises(SystemExit, match="loopback only"):
            check_loopback(host)


def test_the_csrf_token_is_tied_to_the_session(session: Session) -> None:
    from biokids_core.db import PanelSession

    one = auth.AuthState(parent=None, session=PanelSession(token_hash="a" * 64, parent_id=1))
    two = auth.AuthState(parent=None, session=PanelSession(token_hash="b" * 64, parent_id=1))
    assert auth.csrf_token(one) != auth.csrf_token(two)
    assert not auth.csrf_ok(one, auth.csrf_token(two))
    assert auth.csrf_ok(one, auth.csrf_token(one))


# ----------------------------------------------- the gates, adversarially
#
# Both of these were found by reading the code rather than by a failing test,
# and both were verified against the real app before being fixed.


def test_the_wizard_cannot_be_re_run_to_take_over_the_panel(
    client: TestClient, family_ready: None, db: Database
) -> None:
    """`POST /wizard/2` used to overwrite the PIN with no cookie and no session.

    The parent's PIN stopped working, the attacker's started, and a lockout from
    a second earlier was cleared. No cookie was involved, so SameSite bought
    nothing: any page the child's browser loaded could submit it.
    """
    with db.session() as session:
        parent = repo.get_parent(session)
        assert parent is not None
        parent.pin_locked_until = utcnow() + dt.timedelta(minutes=10)

    response = client.post(
        "/wizard/2",
        data={"name": "x", "pin": "424242", "pin_again": "424242"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    with db.session() as session:
        parent = repo.get_parent(session)
        assert parent is not None
        assert security.verify_secret(parent.pin_hash, PIN), "the real PIN still works"
        assert not security.verify_secret(parent.pin_hash, "424242")
        assert parent.pin_locked_until is not None, "and the lockout still stands"


@pytest.mark.parametrize("step", range(1, 11))
def test_no_wizard_step_does_anything_once_setup_is_finished(
    client: TestClient, family_ready: None, db: Database, step: int
) -> None:
    response = client.post(
        f"/wizard/{step}",
        data={
            "lang": "en",
            "pin": "424242",
            "pin_again": "424242",
            "passphrase": "short one here",
            "weekday_quota_min": 1440,
            "display_name": "Intruder",
            "birth_year": dt.date.today().year - 5,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"

    with db.session() as session:
        assert repo.get_or_create_policy(session, 1).weekday_quota_min == 60
        assert len(repo.list_children(session)) == 1


def test_setup_cannot_finish_without_a_passphrase(client: TestClient, db: Database) -> None:
    """Skipping step 3 used to leave no passphrase and no recovery code.

    That silently removed both the gate on turning protection off and the only
    documented way back in after a forgotten PIN.
    """
    client.post("/wizard/1", data={"lang": "tr"})
    client.post("/wizard/2", data={"name": "Anne", "pin": PIN, "pin_again": PIN})

    response = client.post("/wizard/10", data={"voices": "on"}, follow_redirects=False)

    assert response.headers["location"].startswith("/wizard/3")
    with db.session() as session:
        assert not repo.setup_complete(session)


def test_turning_protection_off_needs_the_passphrase(
    client: TestClient, family_ready: None, db: Database
) -> None:
    """D-48: a PIN a ten-year-old watched being typed is not enough."""
    login(client)
    response = client.post(
        "/children/1/time",
        data={
            "weekday_quota_min": 60,
            "end_action": "poweroff",
            "csrf_token": csrf_of(client),
        },
    )

    assert response.status_code == 403
    assert "korumayı kapatıyorsunuz" in response.text
    with db.session() as session:
        assert repo.get_or_create_policy(session, 1).enabled is True


def test_the_passphrase_confirms_it_and_keeps_the_submitted_settings(
    client: TestClient, family_ready: None, db: Database
) -> None:
    login(client)
    response = client.post(
        "/children/1/time",
        data={
            "weekday_quota_min": 45,
            "weekend_quota_min": 90,
            "end_action": "lock",
            "passphrase": PASSPHRASE,
            "csrf_token": csrf_of(client),
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    with db.session() as session:
        policy = repo.get_or_create_policy(session, 1)
        assert policy.enabled is False
        assert policy.weekday_quota_min == 45, "the rest of the form was not lost"
        entries = list(repo.recent_audit(session, limit=5))
    assert entries[0].action == "protection_disabled"
    assert entries[0].severity == "warning"


def test_a_wrong_passphrase_does_not_turn_protection_off(
    client: TestClient, family_ready: None, db: Database
) -> None:
    login(client)
    response = client.post(
        "/children/1/time",
        data={
            "weekday_quota_min": 60,
            "end_action": "poweroff",
            "passphrase": "not the passphrase",
            "csrf_token": csrf_of(client),
        },
    )
    assert response.status_code == 403
    with db.session() as session:
        assert repo.get_or_create_policy(session, 1).enabled is True


def test_an_install_with_no_passphrase_falls_back_to_the_pin(
    client: TestClient, family_ready: None, db: Database
) -> None:
    """Never a dead end (principle #4): a gate with no key locks the parent out."""
    with db.session() as session:
        parent = repo.get_parent(session)
        assert parent is not None
        parent.passphrase_hash = ""

    login(client)
    asked = client.post(
        "/children/1/time",
        data={"weekday_quota_min": 60, "end_action": "lock", "csrf_token": csrf_of(client)},
    )
    assert asked.status_code == 403
    assert "PIN" in asked.text

    client.post(
        "/children/1/time",
        data={
            "weekday_quota_min": 60,
            "end_action": "lock",
            "passphrase": PIN,
            "csrf_token": csrf_of(client),
        },
        follow_redirects=False,
    )
    with db.session() as session:
        assert repo.get_or_create_policy(session, 1).enabled is False


def test_a_passphrase_can_be_set_afterwards(
    client: TestClient, family_ready: None, db: Database
) -> None:
    with db.session() as session:
        parent = repo.get_parent(session)
        assert parent is not None
        parent.passphrase_hash = ""

    login(client)
    response = client.post(
        "/settings/passphrase",
        data={
            "current_pin": PIN,
            "current_passphrase": "",
            "new_passphrase": "yeni bir parola",
            "csrf_token": csrf_of(client),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    with db.session() as session:
        parent = repo.get_parent(session)
        assert parent is not None
        assert security.verify_secret(parent.passphrase_hash, "yeni bir parola")
        assert "passphrase_changed" in [e.action for e in repo.recent_audit(session, limit=5)]


def test_changing_an_existing_passphrase_needs_the_old_one(
    client: TestClient, family_ready: None, db: Database
) -> None:
    login(client)
    client.post(
        "/settings/passphrase",
        data={
            "current_pin": PIN,
            "current_passphrase": "wrong",
            "new_passphrase": "yeni bir parola",
            "csrf_token": csrf_of(client),
        },
        follow_redirects=False,
    )
    with db.session() as session:
        parent = repo.get_parent(session)
        assert parent is not None
        assert security.verify_secret(parent.passphrase_hash, PASSPHRASE)


# ------------------------------------------------- what the buttons report back


def _one_child(daemon: FakeDaemon) -> None:
    daemon.status = {
        "children": [
            {
                "child_id": 1,
                "display_name": "Elif",
                "avatar": "fox",
                "managed": True,
                "online": True,
                "app": "",
                "remaining_seconds": 42 * 60,
                "remaining_minutes": 42,
                "quota_seconds": 60 * 60,
                "granted_seconds": 0,
                "used_seconds": 18 * 60,
                "allow": True,
                "reason_code": "allowed",
                "end_action": "poweroff",
            }
        ]
    }


def test_granting_time_tells_the_parent_it_worked(
    client: TestClient, family_ready: None, daemon: FakeDaemon
) -> None:
    """The three buttons redirect with their outcome, and nothing rendered it."""
    _one_child(daemon)
    login(client)
    response = client.post(
        "/children/1/grant",
        data={"minutes": 10, "csrf_token": csrf_of(client)},
        follow_redirects=True,
    )
    assert "10 dakika eklendi" in response.text
    assert "Elif" in response.text


def test_a_grant_that_never_reached_the_daemon_does_not_look_like_success(
    client: TestClient, family_ready: None, daemon: FakeDaemon
) -> None:
    """The dangerous half. Silence and success were the same page.

    A parent presses "+10 dk" with the daemon stopped, the page reloads
    unchanged, and they walk away believing their child has ten more minutes.
    """
    _one_child(daemon)
    login(client)
    token = csrf_of(client)
    daemon.up = False

    response = client.post(
        "/children/1/grant",
        data={"minutes": 10, "csrf_token": token},
        follow_redirects=True,
    )

    assert "ulaşılamıyor" in response.text, "a failed grant said nothing at all"
    assert "dakika eklendi" not in response.text


def test_locking_and_powering_off_report_back_too(
    client: TestClient, family_ready: None, daemon: FakeDaemon
) -> None:
    _one_child(daemon)
    login(client)
    token = csrf_of(client)

    locked = client.post("/children/1/lock", data={"csrf_token": token}, follow_redirects=True)
    assert "kilitlendi" in locked.text

    off = client.post(
        "/children/1/poweroff",
        data={"confirm": "yes", "csrf_token": token},
        follow_redirects=True,
    )
    assert "kapatılıyor" in off.text


def test_a_plain_today_carries_no_notice(
    client: TestClient, family_ready: None, daemon: FakeDaemon
) -> None:
    """A banner that is always there is a banner nobody reads."""
    _one_child(daemon)
    login(client)
    page = client.get("/today").text
    assert "notice--ok" not in page


# ------------------------------- the outcomes that reached a URL and stopped there


def test_a_rejected_passphrase_change_says_so(client: TestClient, family_ready: None) -> None:
    """`?error=passphrase` was put in a URL that nothing rendered.

    settings.html rendered exactly one error key, `pin`. Everything else -
    a wrong current passphrase, a stale form, a daemon that is down - produced
    the same page back with nothing changed and nothing said, which is
    indistinguishable from success.
    """
    login(client)
    response = client.post(
        "/settings/passphrase",
        data={
            "current_pin": PIN,
            "current_passphrase": "yanlis parola bu",
            "new_passphrase": "yeni gizli kapi mavi",
            "csrf_token": csrf_of(client),
        },
        follow_redirects=True,
    )
    assert "Parola doğru değil" in response.text


def test_a_stale_form_says_what_to_do_about_it(client: TestClient, family_ready: None) -> None:
    """`?error=csrf` reached no template at all."""
    login(client)
    response = client.post(
        "/settings/pin",
        data={"current_pin": PIN, "new_pin": "551234", "csrf_token": "wrong"},
        follow_redirects=True,
    )
    assert "Sayfayı yenileyip" in response.text


def test_saving_time_rules_with_the_daemon_down_does_not_say_saved(
    client: TestClient, family_ready: None, daemon: FakeDaemon
) -> None:
    """A policy the daemon never heard about has not been applied.

    `_call_daemon` returns a bool that this caller threw away, so "Kaydedildi"
    appeared whether or not the reload ever landed - and the child keeps running
    on the old quota while the parent believes the new one is in force.
    """
    login(client)
    token = csrf_of(client)
    daemon.up = False

    response = client.post(
        "/children/1/time",
        data={
            "weekday_quota_min": 45,
            "weekend_quota_min": 90,
            "window": "16:00-20:00",
            "end_action": "lock",
            "enabled": "on",
            "csrf_token": token,
        },
        follow_redirects=True,
    )

    assert "ulaşılamıyor" in response.text
    assert "Kaydedildi" not in response.text


def test_the_login_page_does_not_forward_a_hostile_next(
    client: TestClient, family_ready: None
) -> None:
    """The POST was checked and the GET was not, which is the same hole one step up.

    An already-signed-in parent following a link is redirected straight away,
    and for everyone else the value is rendered into the form as a hidden field
    and posted back.
    """
    login(client)
    response = client.get("/login?next=//evil.example.com/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/today"


def test_the_login_form_carries_a_cleaned_next(client: TestClient, family_ready: None) -> None:
    page = client.get("/login?next=//evil.example.com/").text
    assert "evil.example.com" not in page


def test_adding_a_second_mehmet_does_not_delete_the_first(
    client: TestClient, family_ready: None, db: Database
) -> None:
    """ "Mehmet Ali" and "Mehmet Can" both fold to `mehmet`.

    `create_child` did `existing or Child(...)` and then wrote the new details
    over the row it found, so the older brother vanished from the panel while it
    said a child had been added - and the two shared one Linux account, one
    daily quota and one ledger.
    """
    login(client)
    this_year = dt.date.today().year
    for name in ("Mehmet Ali", "Mehmet Can"):
        client.post(
            "/children",
            data={
                "display_name": name,
                "birth_year": this_year - 7,
                "unix_user": "",
                "avatar": "fox",
                "locale": "tr",
                "csrf_token": csrf_of(client),
            },
            follow_redirects=False,
        )

    with db.session() as session:
        children = repo.list_children(session)
        names = sorted(c.display_name for c in children)
        users = sorted(c.unix_user for c in children)

    assert "Mehmet Ali" in names, f"the first brother was overwritten: {names}"
    assert "Mehmet Can" in names, f"the second brother was not added: {names}"
    assert len(set(users)) == len(users), f"two children share one account: {users}"


def test_a_username_the_parent_typed_is_never_pointed_at_someone_else(
    client: TestClient, family_ready: None
) -> None:
    """A suggested name is ours to adjust; a typed one is theirs."""
    login(client)
    response = client.post(
        "/children",
        data={
            "display_name": "Baska Cocuk",
            "birth_year": dt.date.today().year - 8,
            "unix_user": "elif",
            "avatar": "cat",
            "locale": "tr",
            "csrf_token": csrf_of(client),
        },
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "ba\u015fka bir \u00e7ocu\u011fa ait" in response.text


def test_the_audit_day_filter_uses_the_parents_day(
    client: TestClient, family_ready: None, db: Database
) -> None:
    """Rows are naive UTC; the filter compared them against local midnight.

    In Istanbul (UTC+3) that drops everything between 00:00 and 03:00 - exactly
    the window a parent asking "what happened last night" means. The unfiltered
    list showed the row under today and filtering for today hid it.
    """
    now_local = dt.datetime.now().astimezone()
    early = now_local.replace(hour=1, minute=30, second=0, microsecond=0)
    as_utc = early.astimezone(dt.UTC).replace(tzinfo=None)

    with db.session() as session:
        repo.audit(session, actor="daemon", action="quota_exhausted", target="child:1")
        session.flush()
        row = session.query(AuditLog).order_by(AuditLog.id.desc()).first()
        assert row is not None
        row.ts = as_utc
        session.commit()

    login(client)
    page = client.get(f"/audit?day={early.date().isoformat()}").text
    assert "G\u00fcnl\u00fck s\u00fcre doldu" in _audit_rows(page), (
        "a 01:30 event fell out of its own day"
    )


def test_a_locked_out_parent_is_told_why_the_form_is_dead(
    client: TestClient, family_ready: None
) -> None:
    """The POST explained it; a reload did not.

    Reopening the PWA from the home screen, or coming back on a phone, rendered
    a 200 page with the PIN box and the button both disabled and not one word
    saying why - to a parent who has forgotten their PIN and is already anxious.
    """
    for _ in range(6):
        client.post("/login", data={"pin": "000000"}, follow_redirects=False)

    page = client.get("/login").text

    assert "disabled" in page, "the lockout is not being enforced at all"
    assert "Çok fazla hatalı deneme" in page, "the form is dead and says nothing"


def test_the_settings_pin_notice_is_a_whole_sentence(
    client: TestClient, family_ready: None
) -> None:
    """It borrowed the login sentence, which counts down attempts this form has none of."""
    login(client)
    response = client.post(
        "/settings/pin",
        data={"current_pin": "999999", "new_pin": "551234", "csrf_token": csrf_of(client)},
        follow_redirects=True,
    )
    assert "PIN doğru değil." in response.text
    assert "deneme hakkınız kaldı" not in response.text, (
        "the settings form does not lock, and must not imply that it does"
    )
    assert ".  " not in response.text.split("PIN doğru değil.")[1][:40]


def test_a_rejected_window_does_not_save_the_quotas_anyway(
    client: TestClient, db: Database, daemon: FakeDaemon
) -> None:
    """ "16.00-20.00" - dots, which is how times are often typed in Turkish.

    The two quota lines ran before the parse and `get_db` commits on the way
    out, so the parent got the error page with their quotas already saved and
    their end-action choice silently reverted to the form default, poweroff.
    """
    client.post("/wizard/1", data={"lang": "tr"}, follow_redirects=False)
    client.post(
        "/wizard/2",
        data={"name": "Anne", "pin": PIN, "pin_again": PIN},
        follow_redirects=False,
    )
    page = client.get("/wizard/3").text
    marker = 'name="recovery" value="'
    start = page.index(marker) + len(marker)
    recovery = page[start : page.index('"', start)]
    client.post(
        "/wizard/3",
        data={"passphrase": PASSPHRASE, "recovery": recovery, "written_down": "on"},
        follow_redirects=False,
    )
    client.post(
        "/wizard/4",
        data={
            "display_name": "Elif",
            "birth_year": dt.date.today().year - 6,
            "unix_user": "elif",
            "avatar": "fox",
            "locale": "tr",
        },
        follow_redirects=False,
    )

    # A first, valid submission: the parent chose "lock".
    client.post(
        "/wizard/6",
        data={
            "weekday_quota_min": 30,
            "weekend_quota_min": 30,
            "window": "16:00-20:00",
            "end_action": "lock",
        },
        follow_redirects=False,
    )

    # Then they come back to raise the quota and mistype the window.
    response = client.post(
        "/wizard/6",
        data={
            "weekday_quota_min": 45,
            "weekend_quota_min": 90,
            "window": "16.00-20.00",
            "end_action": "lock",
        },
        follow_redirects=False,
    )
    assert response.status_code == 200, "a bad window must not redirect onwards"

    with db.session() as session:
        child = repo.get_child_by_user(session, "elif")
        assert child is not None
        policy = repo.get_or_create_policy(session, child.id)
        assert policy.weekday_quota_min != 45, "the rejected submission saved its quotas anyway"
        assert policy.end_action != "poweroff", (
            "the parent's end-action choice was reverted to the form default"
        )


# --------------------------------------------- whose account a child gets (D-155)


def test_a_parent_cannot_make_their_own_account_a_childs(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Typed by mistake, it would hand the parent's session to the kiosk and the quota."""
    monkeypatch.setattr(wizard, "account_is_administrative", lambda user: user == "bilal")

    with db.session() as session:
        child, error = wizard.create_child(
            session, display_name="Elif", birth_year=dt.date.today().year - 6, unix_user="bilal"
        )
        assert child is None
        assert error == "child.unix_user.admin"
        assert repo.get_child_by_user(session, "bilal") is None


def test_a_suggested_name_steps_past_somebody_elses_account(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child called Bilal, on a machine whose parent logs in as `bilal`."""
    monkeypatch.setattr(wizard, "account_exists", lambda user: user == "bilal")
    monkeypatch.setattr(wizard, "is_child_account", lambda _user: False)

    with db.session() as session:
        child, error = wizard.create_child(
            session, display_name="Bilal", birth_year=dt.date.today().year - 6
        )
        assert error == ""
        assert child is not None
        assert child.unix_user == "bilal2"


def test_a_suggested_name_reuses_a_childs_account_from_an_earlier_install(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A purge keeps the accounts (and the drawings); a reinstall finds them again."""
    monkeypatch.setattr(wizard, "account_exists", lambda user: user == "elif")
    monkeypatch.setattr(wizard, "is_child_account", lambda user: user == "elif")

    with db.session() as session:
        child, _error = wizard.create_child(
            session, display_name="Elif", birth_year=dt.date.today().year - 6
        )
        assert child is not None
        assert child.unix_user == "elif"


# ------------------------------------------- Tailscale's approval page (D-157)

APPROVAL = "https://login.tailscale.com/f/serve?node=nH6HrHEzpo11CNTRL"


def test_the_parent_is_shown_the_page_that_turns_serve_on(
    client: TestClient, family_ready: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a new tailnet the panel said only "could not be changed" (D-157)."""
    from biokids_core import remote

    running = remote.RemoteStatus(state=remote.RemoteState.RUNNING)
    monkeypatch.setattr(remote, "detect", lambda config=None: running)
    monkeypatch.setattr(
        remote,
        "enable",
        lambda port=None, config=None: remote.RemoteResult(
            ok=False, error=remote.RemoteError.NEEDS_APPROVAL, approval_url=APPROVAL
        ),
    )
    login(client)

    response = client.post(
        "/settings/remote",
        data={"enable": "on", "csrf_token": csrf_of(client)},
        follow_redirects=False,
    )
    # D-169: phone access has a page of its own, which shows the approval.
    assert response.headers["location"] == "/settings/phone?error=remote_approval"

    page = client.get(response.headers["location"]).text
    assert f'href="{APPROVAL}"' in page.replace("&amp;", "&")
    assert "onayınızı bekliyor" in page


def test_a_stored_link_that_is_not_tailscales_is_never_shown(
    client: TestClient, family_ready: None, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from biokids_core import remote

    running = remote.RemoteStatus(state=remote.RemoteState.RUNNING)
    monkeypatch.setattr(remote, "detect", lambda config=None: running)
    with db.session() as session:
        repo.set_setting(
            session, remote.SETTING_APPROVAL_URL, "https://evil.example/f/serve?node=abc"
        )
        session.commit()
    login(client)

    page = client.get("/settings").text
    assert "evil.example" not in page


def test_the_panel_does_not_hold_a_shutdown_for_an_open_phone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """D-160: the Today screen's event stream never closes on its own.

    Without a bound uvicorn waited for it on every stop, and a shutdown pressed
    on the phone sat on "Job biokids-panel.service/stop running" for systemd's
    full ninety seconds.
    """
    from biokids_panel import __main__ as panel_main

    seen: dict[str, Any] = {}
    monkeypatch.setattr(panel_main.uvicorn, "run", lambda *_a, **kw: seen.update(kw))
    monkeypatch.setattr(panel_main, "set_config", lambda _config: None)
    # Not the real app: built from the default paths it opens the machine's own
    # /var/lib/biokids/biokids.db, which fails on a machine with BioKids
    # installed and creates one on a machine without.
    monkeypatch.setattr(panel_main, "create_app", lambda _config: object())

    panel_main.main(["--config", str(tmp_path / "nonexistent.toml")])

    assert 0 < seen["timeout_graceful_shutdown"] <= 5


def test_the_launcher_does_not_hold_a_logout_either() -> None:
    from biokids_shell.__main__ import SHUTDOWN_GRACE_SECONDS

    assert 0 < SHUTDOWN_GRACE_SECONDS <= 5


# ------------------------------------------------------------ apps (D-164)


def test_the_apps_screen_lists_apps_and_sites_and_what_is_ticked(
    client: TestClient, family_ready: None, db: Database
) -> None:
    """Until D-164 only the wizard asked, so a new catalog site could never be added."""
    from biokids_core.catalog import enable_for_child, load_catalog, sync_catalog

    with db.session() as session:
        sync_catalog(session, load_catalog())
        enable_for_child(session, 1, ["eba"])
    login(client)

    page = client.get("/children/1/apps").text

    assert "Uygulamalar ve siteler" in page
    for name in ("KÜRE Çocuk", "Bilim Çocuk", "Meraklı Minik", "YouTube Kids", "Kodlama"):
        assert name in page, name
    assert re.search(r'value="eba"\s+checked', page), "a ticked site shows ticked"
    assert not re.search(r'value="kure-cocuk"\s+checked', page)
    assert 'value="volume"' not in page, "volume, brightness and shutdown are not optional"


def test_saving_apps_sets_exactly_the_ticked_tiles(
    client: TestClient, family_ready: None, db: Database, daemon: FakeDaemon
) -> None:
    login(client)
    response = client.post(
        "/children/1/apps",
        data={
            "apps": ["eba", "youtube-kids", "kure-cocuk", "no-such-app"],
            "csrf_token": csrf_of(client),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("?saved=1")

    with db.session() as session:
        tiles = {app.slug for _link, app in repo.list_child_apps(session, 1)}
        actions = [e.action for e in repo.recent_audit(session, limit=10)]
    assert tiles == {"eba", "youtube-kids", "kure-cocuk", "volume", "brightness", "shutdown"}
    assert "apps_changed" in actions
    assert daemon.ran("reload"), "the browser policy is rewritten from the enabled sites"


def test_unticking_everything_still_leaves_the_shutdown_tile(
    client: TestClient, family_ready: None, db: Database
) -> None:
    login(client)
    client.post(
        "/children/1/apps",
        data={"apps": ["eba"], "csrf_token": csrf_of(client)},
        follow_redirects=False,
    )
    client.post(
        "/children/1/apps", data={"csrf_token": csrf_of(client)}, follow_redirects=False
    )

    with db.session() as session:
        tiles = {app.slug for _link, app in repo.list_child_apps(session, 1)}
    assert tiles == {"volume", "brightness", "shutdown"}


def test_the_apps_screen_needs_a_csrf_token(
    client: TestClient, family_ready: None, db: Database
) -> None:
    login(client)
    response = client.post("/children/1/apps", data={"apps": ["eba"]}, follow_redirects=False)
    assert response.status_code == 303
    assert "error=csrf" in response.headers["location"]
    with db.session() as session:
        assert not repo.list_child_apps(session, 1)


def test_youtube_kids_is_offered_but_never_ticked_for_a_parent() -> None:
    """Google's, with ads, and asks a parent to set it up: a parent chooses it."""
    from biokids_core.catalog import load_catalog

    catalog = load_catalog()
    offered = {e.slug for e in catalog.for_age(6)}
    ticked = {e.slug for e in catalog.suggested_for_age(6)}
    assert {"youtube-kids", "pbs-kids"} <= offered
    assert not {"youtube-kids", "pbs-kids"} & ticked
    assert "merakli-minik" in ticked


def test_the_phones_cache_is_renamed_when_the_files_change(client: TestClient) -> None:
    """A fixed cache name kept a phone on the first stylesheet it ever saw."""
    from biokids_panel.app import static_fingerprint

    script = client.get("/sw.js").text
    assert '"biokids-shell-v1"' not in script
    assert f'"biokids-shell-{static_fingerprint()}"' in script


def test_panel_static_files_are_checked_before_every_use(client: TestClient) -> None:
    assert client.get("/static/panel.css").headers["cache-control"] == "no-cache"


# --------------------------------------------------------- internet (D-167)


def test_a_new_child_gets_the_open_internet_by_default() -> None:
    """The owner's default: open, with harmful sites closed by DNS (D-167)."""
    from biokids_core.types import AgeBand, FilterMode

    assert repo.default_filter_mode(AgeBand.YOUNG) is FilterMode.BLOCKLIST
    assert repo.default_filter_mode(AgeBand.OLDER) is FilterMode.BLOCKLIST


def test_the_internet_screen_saves_the_mode_and_reloads(
    client: TestClient, family_ready: None, db: Database, daemon: FakeDaemon
) -> None:
    login(client)
    page = client.get("/children/1/internet").text
    assert "Açık internet" in page and "Sadece seçtiğim siteler" in page

    response = client.post(
        "/children/1/internet",
        data={"mode": "allowlist", "csrf_token": csrf_of(client)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with db.session() as session:
        assert repo.get_or_create_policy(session, 1).filter_mode == "allowlist"
        actions = [e.action for e in repo.recent_audit(session, limit=5)]
    assert "filter_mode_changed" in actions
    assert daemon.ran("reload")


def test_an_unknown_mode_falls_back_to_the_safe_default(
    client: TestClient, family_ready: None, db: Database
) -> None:
    login(client)
    client.post(
        "/children/1/internet",
        data={"mode": "everything", "csrf_token": csrf_of(client)},
        follow_redirects=False,
    )
    with db.session() as session:
        assert repo.get_or_create_policy(session, 1).filter_mode == "blocklist"


def test_every_quota_field_accepts_twenty_minutes() -> None:
    """The exit criterion's quota is 20 minutes. The wizard's fields had
    `step="15"`, so the browser refused 20 and the parent could not go on;
    the time screen had 5. Every quota field takes any multiple of 5."""
    from pathlib import Path

    import biokids_panel

    templates = Path(biokids_panel.__file__).parent / "templates"
    fields = []
    for path in templates.rglob("*.html"):
        for tag in re.findall(r"<input[^>]*_quota_min[^>]*>", path.read_text(encoding="utf-8")):
            step = re.search(r'step="([0-9.]+)"', tag)
            fields.append((path.name, float(step.group(1)) if step else 1.0))
    assert len(fields) >= 4, fields
    for name, step in fields:
        assert 20 % step == 0 and 5 % step == 0, f"{name}: step {step} refuses 20 or 5"


# ------------------------------------------------ phone access, one button (D-169)


def test_with_no_tailscale_the_button_starts_the_setup(
    client: TestClient, family_ready: None, daemon: FakeDaemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It used to say "install Tailscale first" and offer no button at all."""
    from biokids_core import remote

    absent = remote.RemoteStatus(state=remote.RemoteState.ABSENT)
    monkeypatch.setattr(remote, "detect", lambda config=None: absent)
    login(client)
    assert 'action="/settings/remote"' in client.get("/settings").text

    response = client.post(
        "/settings/remote",
        data={"enable": "on", "csrf_token": csrf_of(client)},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/settings/phone"
    assert daemon.ran("remote_setup")


def test_the_phone_page_follows_the_setup_to_the_sign_in(
    client: TestClient, family_ready: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from biokids_core import remote

    url = "https://login.tailscale.com/a/9edb3e9016be4"
    waiting = remote.RemoteStatus(state=remote.RemoteState.LOGGED_OUT, auth_url=url)
    monkeypatch.setattr(remote, "detect", lambda config=None: waiting)
    monkeypatch.setattr(remote, "setup_state", lambda runner=None: "running")
    login(client)

    page = client.get("/settings/phone").text
    assert 'http-equiv="refresh"' in page, "it refreshes itself while the setup runs"
    assert f'href="{url}"' in page
    assert "<svg" in page, "and the sign-in page as a QR code, for the phone"
    assert 'action="/settings/remote"' not in page, "no second press while it runs"


def test_the_settings_screen_itself_never_refreshes(
    client: TestClient, family_ready: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It has a PIN form on it."""
    from biokids_core import remote

    monkeypatch.setattr(remote, "setup_state", lambda runner=None: "running")
    login(client)
    assert 'http-equiv="refresh"' not in client.get("/settings").text


def test_a_failed_setup_is_said_and_can_be_tried_again(
    client: TestClient, family_ready: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from biokids_core import remote

    monkeypatch.setattr(remote, "setup_state", lambda runner=None: "failed")
    login(client)
    page = client.get("/settings/phone").text
    assert "kurulamadı" in page
    assert "Tekrar dene" in page


# ------------------------------------------- the parent's way in (D-170)


def test_the_parent_panel_is_in_the_applications_menu(config: Config) -> None:
    """Nobody should have to know that the panel lives at 127.0.0.1:7878."""
    import configparser
    from pathlib import Path

    entry = (
        Path(__file__).resolve().parent.parent / "debian" / "parent" / "biokids-panel.desktop"
    )
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    parser.read_string(entry.read_text(encoding="utf-8"))
    desktop = parser["Desktop Entry"]
    assert desktop["Name[tr]"] == "BioKids Ebeveyn Paneli"
    assert desktop["Exec"] == f"xdg-open http://127.0.0.1:{config.panel.port}/"
    assert desktop["Icon"] == "biokids"


def test_the_wizard_opens_by_itself_only_for_a_parent_and_only_until_setup_is_done() -> None:
    from pathlib import Path

    script = (
        Path(__file__).resolve().parent.parent / "debian" / "parent" / "biokids-open-setup"
    ).read_text(encoding="utf-8")
    assert '*" biokids-child "*) exit 0' in script, "never in a child's session"
    assert '"/wizard" in location' in script, "only while / still leads to the wizard"
    assert script.rstrip().endswith('exec xdg-open "http://127.0.0.1:7878/wizard/1"')


# ------------------------------------------------------- D-173 requests


def test_the_requests_screen_answers_in_one_tap(
    client: TestClient, family_ready: None, db: Database, daemon: FakeDaemon
) -> None:
    from biokids_core.db import AccessRequest

    with db.session() as session:
        session.add(AccessRequest(child_id=1, url="", kind="time", status="pending"))
    login(client)

    page = client.get("/requests").text
    assert "/requests/1/decide" in page

    client.post(
        "/requests/1/decide",
        data={"approve": "yes", "minutes": 30, "csrf_token": csrf_of(client)},
        follow_redirects=False,
    )
    op, args = daemon.calls[-1]
    assert op == "decide_request"
    assert args["approve"] is True and args["minutes"] == 30


def test_unlock_needs_no_time_to_be_given(
    client: TestClient, family_ready: None, daemon: FakeDaemon
) -> None:
    login(client)
    client.post(
        "/children/1/unlock", data={"csrf_token": csrf_of(client)}, follow_redirects=False
    )
    assert daemon.ran("unlock_now") and not daemon.ran("grant_time")


# ------------------------------------------------- D-174 recordings


def test_the_panel_keeps_a_recording_sent_from_the_phone(
    client: TestClient, family_ready: None, config: Config
) -> None:
    from biokids_core import recorded_voice
    from test_recorded_voice import a_wav

    login(client)
    token = csrf_of(client)
    response = client.post(
        "/voices/warn_1?text=Son%20dakika",
        content=a_wav(1.5),
        headers={"content-type": "audio/wav", "x-csrf-token": token},
    )
    assert response.json() == {"ok": True}
    found = recorded_voice.lookup(config.paths.recorded_voice_dir, "tr", "warn_1")
    assert found is not None and found.text == "Son dakika"
    assert "/voices/tr/warn_1.wav" in client.get("/voices").text


def test_a_recording_without_the_token_is_refused(
    client: TestClient, family_ready: None
) -> None:
    from test_recorded_voice import a_wav

    login(client)
    response = client.post(
        "/voices/warn_1", content=a_wav(), headers={"content-type": "audio/wav"}
    )
    assert response.status_code == 403
