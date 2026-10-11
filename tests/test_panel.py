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
import html
import re
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from biokids_core import repo, security
from biokids_core.catalog import load_catalog
from biokids_core.config import Config
from biokids_core.db import AuditLog, Database, Parent, Policy
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
    client: TestClient, db: Database, daemon: FakeDaemon, config: Config
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
    done = client.post("/wizard/9", data={"voices": "on"}, follow_redirects=False)
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
        # What was ticked and can start here, and always the three built-ins:
        # the step listed shutdown as a box a parent could untick (D-164).
        installed = {e.slug for e in load_catalog(config=config).entries if e.available()}
        enabled = {
            app.slug for link, app in repo.list_child_apps(session, child.id) if link.enabled
        }
        assert enabled == ({"tuxpaint", "gcompris"} & installed) | {
            "volume",
            "brightness",
            "shutdown",
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
    assert "hem başlangıcı hem bitişi" in response.text
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


@pytest.mark.parametrize("step", range(1, 10))
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

    response = client.post("/wizard/9", data={"voices": "on"}, follow_redirects=False)

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


def test_a_removed_child_added_back_gets_their_old_account(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not `elif2`: the hidden account still holds Elif's drawings (D-191)."""
    monkeypatch.setattr(wizard, "account_exists", lambda user: user == "elif")
    monkeypatch.setattr(wizard, "is_child_account", lambda _user: False)
    monkeypatch.setattr(wizard, "is_removed_child_account", lambda user: user == "elif")

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


# ------------------------------------------- what an upgrade added (D-207)

D203 = frozenset(
    {
        "marble",
        "ktuberling",
        "blinken",
        "palapeli",
        "kgeography",
        "kbruch",
        "kturtle",
        "gnome-chess",
    }
)


def _upgraded(db: Database, *, age: int = 5) -> None:
    """Elif, set up with Boya before D-203, after the upgrade that brought its
    eight apps."""
    from biokids_core.catalog import BUNDLED_CATALOG, Catalog, enable_for_child, sync_catalog

    bundled = load_catalog(BUNDLED_CATALOG)
    with db.session() as session:
        child = repo.get_child(session, 1)
        assert child is not None
        child.birth_year = dt.date.today().year - age
        before = Catalog(tuple(e for e in bundled if e.slug not in D203))
        sync_catalog(session, before, now=utcnow() - dt.timedelta(days=90))
        enable_for_child(session, 1, ["tuxpaint"])
        sync_catalog(session, bundled, now=utcnow() - dt.timedelta(hours=1))


def test_the_apps_screen_marks_what_an_upgrade_added(
    client: TestClient, family_ready: None, db: Database
) -> None:
    _upgraded(db)
    login(client)

    page = html.unescape(client.get("/children/1/apps").text)

    # Not news: they are not on her screen, and the boxes below are how to add them.
    assert (
        "Bu çocuğun yaşına uygun 2 yeni uygulama henüz ekranında yok: Patates Kafa, Hafıza."
        " Eklemek için işaretleyin." in page
    )
    assert page.count('class="badge-new"') == len(D203), "every new tile says so"
    tuxpaint = page[page.index('value="tuxpaint"') : page.index('value="tuxmath"')]
    assert "badge-new" not in tuxpaint, "an old tile does not"


def test_the_children_screen_says_which_child_has_new_apps(
    client: TestClient, family_ready: None, db: Database
) -> None:
    from biokids_core.catalog import enable_for_child

    _upgraded(db)
    with db.session() as session:
        # Hafıza reached her another way: a request the parent said yes to.
        enable_for_child(session, 1, ["blinken"], replace=False)
    login(client)

    page = html.unescape(client.get("/children").text)

    # A link to the boxes rather than "tick it", which has nothing to tick here.
    line = "Bu çocuğun yaşına uygun yeni bir uygulama henüz ekranında yok: Patates Kafa."
    assert f'<a href="/children/1/apps">{line}</a>' in page
    assert "işaretleyin" not in page


def test_a_parent_who_saved_the_apps_is_not_asked_again(
    client: TestClient, family_ready: None, db: Database
) -> None:
    """Leaving them unticked is an answer; the "Yeni" on each tile stays."""
    _upgraded(db)
    login(client)
    client.post(
        "/children/1/apps",
        data={"apps": ["tuxpaint"], "csrf_token": csrf_of(client)},
        follow_redirects=False,
    )

    apps = client.get("/children/1/apps").text
    assert "henüz ekranında yok" not in apps
    assert apps.count('class="badge-new"') == len(D203)
    assert "henüz ekranında yok" not in client.get("/children").text


def test_only_her_own_save_since_the_upgrade_answers_the_line(
    client: TestClient, family_ready: None, db: Database
) -> None:
    """A save from before the upgrade could not have looked at apps that were
    not there yet, and her brother's save was about his tiles, not hers: the
    line about Elif's stays until Elif's apps are saved again."""
    from sqlalchemy import select

    from biokids_core.catalog import new_apps
    from biokids_core.db import Child

    _upgraded(db)
    with db.session() as session:
        arrived = min(new_apps(session).values())
        can = Child(unix_user="can", display_name="Can", birth_year=dt.date.today().year - 5)
        session.add(can)
        session.flush()
        session.add(Policy(child_id=can.id))
        can_id = can.id
    login(client)
    line = "Bu çocuğun yaşına uygun 2 yeni uygulama henüz ekranında yok: Patates Kafa, Hafıza."

    # Elif's apps saved on the panel, as it was the day before the upgrade.
    client.post(
        "/children/1/apps",
        data={"apps": ["tuxpaint"], "csrf_token": csrf_of(client)},
        follow_redirects=False,
    )
    saved_before = arrived - dt.timedelta(days=1)
    with db.session() as session:
        saved = session.scalars(
            select(AuditLog).where(
                AuditLog.action == "apps_changed", AuditLog.target == "child:1"
            )
        ).one()
        assert saved.actor == "parent:1"
        saved.ts = saved_before
    children = html.unescape(client.get("/children").text)
    assert children.count("henüz ekranında yok") == 2, "one line each, Elif's still there"
    # Can's apps saved now, after it.
    client.post(
        f"/children/{can_id}/apps",
        data={"apps": ["tuxpaint"], "csrf_token": csrf_of(client)},
        follow_redirects=False,
    )

    with db.session() as session:
        assert repo.apps_saved_at(session, 1) == saved_before, "not Can's save"
        can_saved = repo.apps_saved_at(session, can_id)
        assert can_saved is not None and can_saved > arrived
    assert line in html.unescape(client.get("/children/1/apps").text)
    children = html.unescape(client.get("/children").text)
    assert f'<a href="/children/1/apps">{line}</a>' in children
    assert children.count("henüz ekranında yok") == 1, "Can's own save answered his"


def test_the_line_is_in_the_familys_language(
    client: TestClient, family_ready: None, db: Database
) -> None:
    _upgraded(db, age=10)
    with db.session() as session:
        family = repo.get_family(session)
        assert family is not None
        family.locale = "en"
    login(client)

    page = html.unescape(client.get("/children/1/apps").text)

    assert (
        "3 new apps suit this child's age but are not on their screen yet:"
        " Globe, Maps, Fractions. Tick them to add them." in page
    )
    assert ">New</span>" in page


def test_a_fresh_install_marks_nothing_new(
    client: TestClient, family_ready: None, db: Database
) -> None:
    from biokids_core.catalog import sync_catalog

    with db.session() as session:
        sync_catalog(session, load_catalog())
    login(client)

    assert "badge-new" not in client.get("/children/1/apps").text
    assert "henüz ekranında yok" not in client.get("/children").text


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


def test_the_internet_screen_says_searches_are_already_stopped(
    client: TestClient, family_ready: None
) -> None:
    """D-202: a parent is told a list exists before being asked for words."""
    login(client)
    page = html.unescape(client.get("/children/1/internet").text)
    assert "Aranamayacak kelimeler" in page
    assert "hazır bir listeyle zaten durdurur" in page
    assert "bütün çocuklarınız için geçerlidir" in page
    assert 'action="/children/1/internet/words"' in page


def test_a_parent_adds_search_words_for_the_family(
    client: TestClient, family_ready: None, db: Database, daemon: FakeDaemon
) -> None:
    login(client)
    response = client.post(
        "/children/1/internet/words",
        data={"words": "Kumar\nbahis, KUMAR", "csrf_token": csrf_of(client)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/children/1/internet?saved=1"
    with db.session() as session:
        assert repo.search_words(session) == ["kumar", "bahis"]
        entry = repo.recent_audit(session, limit=1)[0]
    assert entry.action == "search_words_changed"
    assert daemon.ran("reload")

    page = html.unescape(client.get("/children/1/internet").text)
    assert "kumar\nbahis" in page


def test_a_word_that_cannot_be_one_is_left_out_and_the_parent_told(
    client: TestClient, family_ready: None, db: Database
) -> None:
    login(client)
    response = client.post(
        "/children/1/internet/words",
        data={"words": "x\nkumar", "csrf_token": csrf_of(client)},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/children/1/internet?error=internet.words.refused"
    with db.session() as session:
        assert repo.search_words(session) == ["kumar"]
    page = client.get(response.headers["location"]).text
    assert "Bazı kelimeler eklenmedi" in page


def test_search_words_need_the_csrf_token(
    client: TestClient, family_ready: None, db: Database
) -> None:
    login(client)
    client.post("/children/1/internet/words", data={"words": "kumar"}, follow_redirects=False)
    with db.session() as session:
        assert repo.search_words(session) == []


def test_a_refused_search_request_offers_a_talk_and_nothing_to_allow(
    client: TestClient, family_ready: None, db: Database
) -> None:
    from biokids_core.db.models import AccessRequest

    with db.session() as session:
        session.add(AccessRequest(child_id=1, url="", kind="search", status="pending"))
    login(client)
    page = html.unescape(client.get("/requests").text)
    assert "Elif kapalı bir şeyi aramak istedi" in page
    assert "Tamam, konuşacağız" in page
    assert "İzin ver" not in page


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


# ------------------------------------------------- D-196 the natural voice


def _natural_state(
    monkeypatch: pytest.MonkeyPatch, state: str, *, speaks: bool = False
) -> None:
    from biokids_core import natural_voice

    monkeypatch.setattr(natural_voice, "state", lambda _config, runner=None: state)
    monkeypatch.setattr(natural_voice, "natural_speaks", lambda _config: speaks)
    monkeypatch.setattr(natural_voice, "progress", lambda _config: 42)


def test_the_voices_page_says_the_robot_speaks_and_offers_the_natural_voice(
    client: TestClient, family_ready: None
) -> None:
    login(client)
    page = client.get("/voices").text

    assert "robot sesiyle" in page
    assert 'action="/voice-natural"' in page
    assert "Doğal sesi indir (yaklaşık 90 MB)" in page
    assert "ticari olmayan" in page, "the licence, in one sentence, before the button"
    assert "kendi sesinizle kaydedebilirsiniz" in page, "and the parent's own voice"


def test_the_button_starts_the_download_and_lands_on_its_progress(
    client: TestClient, family_ready: None, daemon: FakeDaemon, db: Database
) -> None:
    login(client)
    response = client.post(
        "/voice-natural", data={"csrf_token": csrf_of(client)}, follow_redirects=False
    )

    assert response.headers["location"] == "/voice-natural?started=1"
    assert daemon.ran("voice_setup")
    page = client.get("/voice-natural?started=1").text
    # The unit may not have started yet: wait once, never offer the button again.
    assert 'http-equiv="refresh" content="3;url=/voice-natural"' in page
    assert 'action="/voice-natural"' not in page
    with db.session() as session:
        assert session.query(AuditLog).filter_by(action="voice_download").count() == 1


def test_the_download_needs_the_form_token(
    client: TestClient, family_ready: None, daemon: FakeDaemon
) -> None:
    login(client)
    response = client.post(
        "/voice-natural", data={"csrf_token": "forged"}, follow_redirects=False
    )
    assert "error=csrf" in response.headers["location"]
    assert not daemon.ran("voice_setup")


def test_a_download_the_daemon_could_not_start_is_said_plainly(
    client: TestClient, family_ready: None, daemon: FakeDaemon
) -> None:
    login(client)
    token = csrf_of(client)
    daemon.up = False
    response = client.post("/voice-natural", data={"csrf_token": token}, follow_redirects=False)
    assert response.headers["location"] == "/voices?error=voices.natural.not_started"


def test_while_it_downloads_only_its_own_page_refreshes(
    client: TestClient, family_ready: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/voices has a sentence field per line and maybe a recording under way."""
    _natural_state(monkeypatch, "running")
    login(client)

    progress = client.get("/voice-natural").text
    assert "İndiriliyor: %42" in progress
    assert '<meta http-equiv="refresh" content="5">' in progress

    voices = client.get("/voices").text
    assert "İndiriliyor: %42" in voices
    assert 'href="/voice-natural"' in voices
    assert "http-equiv" not in voices
    assert 'action="/voice-natural"' not in voices


def test_once_installed_the_card_says_the_natural_voice_speaks(
    client: TestClient, family_ready: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _natural_state(monkeypatch, "installed", speaks=True)
    login(client)

    page = client.get("/voices").text
    assert "doğal sesle konuşuyor" in page
    assert "bu bilgisayarda kurulu" in page
    assert 'action="/voice-natural"' not in page
    assert "Hazır." in client.get("/voice-natural").text


def test_a_failed_download_says_so_and_offers_the_button_again(
    client: TestClient, family_ready: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _natural_state(monkeypatch, "failed")
    login(client)

    for path in ("/voices", "/voice-natural"):
        page = client.get(path).text
        assert "İndirme tamamlanamadı" in page, path
        assert 'action="/voice-natural"' in page, path
        assert "http-equiv" not in page, path


def test_the_settings_lead_to_the_voices(client: TestClient, family_ready: None) -> None:
    login(client)
    page = client.get("/settings").text
    assert "doğal bir ses indirin" in page
    assert 'href="/voices"' in page


# ------------------------------------------------------ D-176 time pickers


def test_hours_come_from_the_time_pickers(
    client: TestClient, family_ready: None, db: Database
) -> None:
    login(client)
    response = client.post(
        "/children/1/time",
        data={
            "csrf_token": csrf_of(client),
            "weekday_quota_min": 60,
            "weekend_quota_min": 90,
            "weekday_from": ["07:30", "16:00"],
            "weekday_to": ["08:15", "19:00"],
            "weekend_from": ["", ""],
            "weekend_to": ["", ""],
            "day_custom": ["2"],
            "day_quota": ["", "", "30", "", "", "", ""],
            "day_from": ["", "", "18:00", "", "", "", ""],
            "day_to": ["", "", "19:00", "", "", "", ""],
            "enabled": "on",
            "end_action": "lock",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with db.session() as session:
        policy = session.get(Policy, 1)
        assert policy is not None
        assert policy.windows["weekday"] == ["07:30-08:15", "16:00-19:00"]
        assert policy.windows["weekend"] == []
        assert policy.schedule == {2: {"quota": 30, "windows": ["18:00-19:00"]}}


def test_half_a_range_is_said_not_guessed(client: TestClient, family_ready: None) -> None:
    login(client)
    response = client.post(
        "/children/1/time",
        data={
            "csrf_token": csrf_of(client),
            "weekday_from": ["16:00", ""],
            "weekday_to": ["", ""],
            "enabled": "on",
        },
    )
    assert response.status_code == 400


# ------------------------------------------------------------ D-177 mail


def test_the_mail_password_is_kept_when_left_empty(
    client: TestClient, family_ready: None, db: Database, config: Config
) -> None:
    from biokids_core import mail

    login(client)
    form = {
        "csrf_token": csrf_of(client),
        "host": "smtp.example.com",
        "port": 587,
        "security": "starttls",
        "username": "anne@example.com",
        "password": "app-password",
        "recipients": "anne@example.com",
        "alerts": "on",
    }
    client.post("/settings/mail", data=form, follow_redirects=False)
    form["password"] = ""
    form["csrf_token"] = csrf_of(client)
    client.post("/settings/mail", data=form, follow_redirects=False)

    with db.session() as session:
        saved = mail.load_settings(session, config)
    assert saved is not None and saved.password == "app-password"
    assert saved.weekly is False and saved.alerts is True
    assert "app-password" not in client.get("/settings").text


def test_a_test_mail_says_whether_it_went(
    client: TestClient, family_ready: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from biokids_core import mail

    def refuse(*_a: object, **_k: object) -> None:
        raise mail.MailError("SMTPAuthenticationError: 535")

    monkeypatch.setattr(mail, "send", refuse)
    login(client)
    response = client.post(
        "/settings/mail",
        data={
            "csrf_token": csrf_of(client),
            "action": "test",
            "host": "smtp.example.com",
            "username": "anne@example.com",
            "password": "wrong",
            "recipients": "anne@example.com",
        },
        follow_redirects=False,
    )
    assert "error=mail.failed" in response.headers["location"]


# ---------------------------------------------- editing and removing (D-191)


def test_a_childs_details_can_be_changed(
    client: TestClient,
    family_ready: None,
    db: Database,
    daemon: FakeDaemon,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from biokids_core.provision import ProvisionResult

    shown: list[str] = []

    def fake_provision(_session: Session, child: Any) -> ProvisionResult:
        shown.append(child.display_name)
        return ProvisionResult(unix_user=child.unix_user)

    monkeypatch.setattr(wizard, "provision_child", fake_provision)
    login(client)
    assert "Elif" in client.get("/children/1/edit").text

    response = client.post(
        "/children/1/edit",
        data={
            "display_name": "Elif  Nur",
            "birth_year": dt.date.today().year - 9,
            "avatar": "owl",
            "locale": "en",
            "supervision_indicator": "small",
            "csrf_token": csrf_of(client),
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith("/children/1/edit?saved=1")
    with db.session() as session:
        child = repo.get_child(session, 1)
        assert child is not None
        assert (child.display_name, child.avatar, child.locale) == ("Elif Nur", "owl", "en")
        assert child.supervision_indicator == "small"
        assert child.age_band == "8-10"
        assert repo.recent_audit(session, limit=5)[0].action == "child_changed"
    assert shown == ["Elif Nur"], "the login screen keeps the old name and picture"
    assert daemon.ran("reload")


def test_an_unknown_indicator_or_avatar_is_ignored(
    client: TestClient, family_ready: None, db: Database
) -> None:
    login(client)
    client.post(
        "/children/1/edit",
        data={
            "display_name": "Elif",
            "birth_year": 2020,
            "avatar": "../../etc/passwd",
            "supervision_indicator": "covert",
            "csrf_token": csrf_of(client),
        },
    )
    with db.session() as session:
        child = repo.get_child(session, 1)
        assert child is not None
        assert child.avatar == "fox"
        assert child.supervision_indicator == "badge"


def _fake_unprovision(monkeypatch: pytest.MonkeyPatch, problems: list[str]) -> list[str]:
    from biokids_core.provision import ProvisionResult
    from biokids_panel.routes import children

    asked: list[str] = []

    def fake(user: str, _config: Any = None) -> ProvisionResult:
        asked.append(user)
        return ProvisionResult(unix_user=user, account_existed=True, problems=list(problems))

    monkeypatch.setattr(children, "unprovision", fake)
    return asked


def test_removing_a_child_needs_the_passphrase(
    client: TestClient, family_ready: None, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = _fake_unprovision(monkeypatch, [])
    login(client)
    response = client.post(
        "/children/1/delete", data={"csrf_token": csrf_of(client)}, follow_redirects=False
    )
    assert response.headers["location"].endswith("error=passphrase")
    wrong = client.post(
        "/children/1/delete",
        data={"passphrase": "not it", "csrf_token": csrf_of(client)},
        follow_redirects=False,
    )
    assert wrong.headers["location"].endswith("error=passphrase")
    assert asked == []
    with db.session() as session:
        assert repo.get_child(session, 1) is not None


def test_a_child_at_the_machine_is_not_removed(
    client: TestClient,
    family_ready: None,
    db: Database,
    daemon: FakeDaemon,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked = _fake_unprovision(monkeypatch, [])
    _one_child(daemon)
    login(client)
    response = client.post(
        "/children/1/delete",
        data={"passphrase": PASSPHRASE, "csrf_token": csrf_of(client)},
        follow_redirects=False,
    )
    assert response.headers["location"].endswith("error=child.remove.logged_in")
    assert asked == []
    with db.session() as session:
        assert repo.get_child(session, 1) is not None


def test_removing_a_child_deletes_everything_that_was_theirs(
    client: TestClient,
    family_ready: None,
    db: Database,
    daemon: FakeDaemon,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked = _fake_unprovision(monkeypatch, [])
    login(client)
    response = client.post(
        "/children/1/delete",
        data={"passphrase": PASSPHRASE, "csrf_token": csrf_of(client)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith("/children?saved=removed")
    assert asked == ["elif"]
    with db.session() as session:
        assert repo.get_child(session, 1) is None
        assert session.get(Policy, 1) is None, "the policy outlived its child"
        entry = repo.recent_audit(session, limit=5)[0]
        assert entry.action == "child_removed"
    assert daemon.ran("reload")


def test_a_child_whose_account_could_not_be_changed_is_kept(
    client: TestClient, family_ready: None, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never half-way: an account still entered by picture, with no row to supervise it."""
    _fake_unprovision(monkeypatch, ["gpasswd: permission denied"])
    login(client)
    response = client.post(
        "/children/1/delete",
        data={"passphrase": PASSPHRASE, "csrf_token": csrf_of(client)},
        follow_redirects=False,
    )
    assert response.headers["location"].endswith("error=child.remove.failed")
    with db.session() as session:
        assert repo.get_child(session, 1) is not None
        assert repo.recent_audit(session, limit=5)[0].action == "child_remove_failed"


# ------------------------------------------------------------- history (D-192)


def test_the_history_shows_a_days_minutes_apps_sites_and_requests(
    client: TestClient, family_ready: None, db: Database
) -> None:
    from biokids_core.db.models import AccessRequest, LedgerDay, UsageTick

    today = dt.date.today()
    with db.session() as session:
        session.add(LedgerDay(child_id=1, day=today, general_s=1800))
        session.add(LedgerDay(child_id=1, day=today - dt.timedelta(days=3), general_s=600))
        session.add(
            UsageTick(
                child_id=1, day=today, category="games", app_slug="gcompris", seconds=1200
            )
        )
        for host in ("www.trt.net.tr", "trt.net.tr", "tr.wikipedia.org"):
            repo.record_event(session, kind="visit", child_id=1, payload={"host": host})
        session.add(
            AccessRequest(
                child_id=1, url="https://youtube.com/watch", kind="site", status="denied"
            )
        )
        session.commit()

    login(client)
    page = client.get("/history").text

    assert page.count('class="bars__day') == 14
    assert "gcompris" in page
    assert "20 dk" in page
    assert page.count("<code>trt.net.tr</code>") == 1, "one line per site, not per page"
    assert "tr.wikipedia.org" in page
    assert "youtube.com" in page


def test_another_day_of_history_and_a_bad_day_both_render(
    client: TestClient, family_ready: None
) -> None:
    login(client)
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    assert client.get(f"/history?child=1&day={yesterday}").status_code == 200
    assert client.get("/history?day=not-a-day&child=99").status_code == 200
    assert client.get("/history?day=2999-01-01").status_code == 200


# ------------------------------------------------------------ insights (D-206)


def _a_busy_month(db: Database) -> None:
    """Three weeks of use ending yesterday, and the things a week can hold."""
    from biokids_core.db.models import AccessRequest, LedgerDay, UsageHour, UsageTick

    today = dt.date.today()
    with db.session() as session:
        for back in range(1, 22):
            day = today - dt.timedelta(days=back)
            session.add(LedgerDay(child_id=1, day=day, general_s=3600, earned_s=300))
            session.add(
                UsageTick(
                    child_id=1, day=day, category="games", app_slug="supertux", seconds=2400
                )
            )
            session.add(
                UsageTick(
                    child_id=1, day=day, category="learning", app_slug="gcompris", seconds=900
                )
            )
            session.add(UsageHour(child_id=1, day=day, hour=17, seconds=2700))
            session.add(UsageHour(child_id=1, day=day, hour=21, seconds=900))
        for host in ("www.trt.net.tr", "tr.wikipedia.org"):
            repo.record_event(session, kind="visit", child_id=1, payload={"host": host})
        session.add(AccessRequest(child_id=1, url="", kind="time", status="approved"))
        session.add(AccessRequest(child_id=1, url="", kind="time", status="denied"))
        session.commit()


def test_insights_need_a_signed_in_parent(client: TestClient, family_ready: None) -> None:
    response = client.get("/insights", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_insights_without_data_say_so_and_nothing_else(
    client: TestClient, family_ready: None
) -> None:
    login(client)
    page = client.get("/insights").text
    assert "Henüz yeterli veri yok" in page
    assert "hourbars" not in page and "Öne çıkanlar" not in page


def test_insights_show_a_month_in_words_and_bars(
    client: TestClient, family_ready: None, db: Database
) -> None:
    _a_busy_month(db)
    login(client)
    page = html.unescape(client.get("/insights?child=1&period=30d").text)

    assert 'aria-current="page">Son 30 gün' in page
    assert "Öne çıkanlar" in page
    assert "saat 21:00 ile 06:00 arasında" in page, "the late evenings"
    assert "/children/1/time#windows" in page
    assert "oyunlarda geçti" in page and "/children/1/time?more=1#limits" in page
    assert page.count('class="daybars__day') == 30
    # Thirty bars are a few pixels each on a phone: not links, nor offered as such.
    assert "/history?child=1&day=" not in page and "Bir güne dokunun" not in page
    assert page.count('class="hourbars__hour"') == 24
    assert "supertux" in page and "gcompris" in page
    assert "<code>trt.net.tr</code>" in page and "tr.wikipedia.org" in page
    assert "Daha fazla süre" in page and "1 izin verildi · 1 şimdi değil" in page
    assert "insights." not in page, "a catalogue key leaked onto the page"


def test_insights_in_english_and_with_odd_parameters(
    client: TestClient, family_ready: None, db: Database
) -> None:
    _a_busy_month(db)
    login(client)
    page = client.get("/insights?period=last_week&lang=en").text
    assert "Insights" in page and "insights." not in page
    assert client.get("/insights?child=99&period=forever").status_code == 200
    week = html.unescape(client.get("/insights?period=week").text)
    assert 'aria-current="page">Bu hafta' in week
    assert "/history?child=1&day=" in week and "Bir güne dokunun" in week, "a week's bars"


def test_the_average_tile_says_a_large_rise_in_words() -> None:
    """The tile and the highlight share one rule: beyond tripling, no "%900"."""
    from biokids_core import insights
    from biokids_core.i18n import translator
    from biokids_panel.routes.insights import _delta

    monday, thursday = dt.date(2026, 10, 5), dt.date(2026, 10, 8)
    general = {monday - dt.timedelta(days=7 - n): 900 for n in range(3)}
    general |= {monday + dt.timedelta(days=n): 3600 for n in range(3)}
    span = insights.span_for(insights.Period.WEEK, thursday)
    found = insights.summarize(
        insights.Facts(general=general), insights.Period.WEEK, span, thursday
    )
    assert _delta(found, translator("tr")) == {
        "text": "↑ iki katından fazla",
        "vs": "Geçen haftanın aynı günlerine göre",
    }
    assert _delta(found, translator("en"))["text"] == "↑ more than double"  # type: ignore[index]


def test_insights_tell_a_waiting_request_from_an_unanswered_one(
    client: TestClient, family_ready: None, db: Database
) -> None:
    from biokids_core.db.models import AccessRequest

    _a_busy_month(db)
    with db.session() as session:
        session.add(AccessRequest(child_id=1, url="", kind="time", status="pending"))
        session.add(AccessRequest(child_id=1, url="", kind="time", status="pending"))
        session.commit()
    login(client)
    page = html.unescape(client.get("/insights").text)
    assert "1 izin verildi · 1 şimdi değil · 2 bekliyor" in page
    assert "cevapsız" not in page, "a request still open was not left unanswered"


def test_insights_say_where_the_sentences_go(
    client: TestClient, family_ready: None, db: Database, config: Config
) -> None:
    """ "Sent nowhere" is true until the Sunday mail carries the highlights."""
    from biokids_core import mail

    _a_busy_month(db)
    login(client)
    assert "hiçbir yere gönderilmez" in html.unescape(client.get("/insights").text)
    with db.session() as session:
        mail.save_settings(
            session,
            config,
            mail.MailSettings(
                host="smtp.example.com",
                username="anne@example.com",
                recipients=("anne@example.com",),
            ),
        )
        session.commit()
    page = html.unescape(client.get("/insights").text)
    assert "hiçbir yere gönderilmez" not in page
    assert "Pazar akşamı kendi e-posta sunucunuz" in page


def test_insights_are_in_the_menu_and_a_tap_from_history(
    client: TestClient, family_ready: None
) -> None:
    login(client)
    assert 'href="/insights"' in client.get("/today").text
    assert 'href="/insights?child=1"' in client.get("/history").text


def test_today_carries_the_week_in_a_few_sentences(
    client: TestClient, family_ready: None, db: Database
) -> None:
    login(client)
    assert "Bu hafta için henüz söylenecek bir şey yok." in client.get("/today").text

    _a_busy_month(db)
    page = html.unescape(client.get("/today").text)
    card = page[page.index("week-card") :]
    assert 'href="/insights?child=1"' in card
    assert card.count('class="highlight ') <= 3


def test_today_renders_when_the_week_cannot_be_worked_out(
    client: TestClient, family_ready: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A convenience failing must not take the screen that grants time with it."""
    from biokids_core import insights

    def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("a bug in the insights")

    monkeypatch.setattr(insights, "week_highlights", broken)
    login(client)
    response = client.get("/today")
    assert response.status_code == 200
    assert "week-card" not in response.text


def test_a_suggestion_opens_the_advanced_time_settings(
    client: TestClient, family_ready: None
) -> None:
    login(client)
    assert 'class="advanced" open>' in client.get("/children/1/time?more=1").text
    assert 'class="advanced" open>' not in client.get("/children/1/time").text


# ---------------------------------------------------- limits by kind (D-193)


def test_a_limit_per_kind_of_app_is_saved_and_cleared(
    client: TestClient, family_ready: None, db: Database, daemon: FakeDaemon
) -> None:
    from biokids_core.types import Category

    login(client)
    form = {
        "weekday_quota_min": 60,
        "weekend_quota_min": 120,
        "end_action": "lock",
        "enabled": "on",
        "limit_games": "30",
        "limit_web": "  ",
        "limit_learning": "9999",
    }
    response = client.post(
        "/children/1/time", data={**form, "csrf_token": csrf_of(client)}, follow_redirects=False
    )
    assert response.status_code == 303
    with db.session() as session:
        assert repo.load_category_limits(session, 1) == {
            Category.GAMES: 30,
            Category.LEARNING: 24 * 60,
        }
        detail = repo.recent_audit(session, limit=5)[0].detail
        assert detail["after"]["category_limits"] == {"games": 30, "learning": 1440}
    assert 'name="limit_games"' in client.get("/children/1/time").text

    client.post(
        "/children/1/time",
        data={**form, "limit_games": "", "limit_learning": "", "csrf_token": csrf_of(client)},
    )
    with db.session() as session:
        assert repo.load_category_limits(session, 1) == {}


def test_a_limit_that_is_not_a_number_is_refused(
    client: TestClient, family_ready: None, db: Database
) -> None:
    login(client)
    response = client.post(
        "/children/1/time",
        data={
            "weekday_quota_min": 60,
            "enabled": "on",
            "limit_games": "otuz",
            "csrf_token": csrf_of(client),
        },
    )
    assert response.status_code == 400
    with db.session() as session:
        assert repo.load_category_limits(session, 1) == {}


# --------------------------------------------------------- small fixes (D-194)


def test_the_offline_page_carries_nothing_about_the_family(
    client: TestClient, family_ready: None
) -> None:
    """It is cached on the phone: no name, no token, no navigation."""
    login(client)
    page = client.get("/offline")
    assert page.status_code == 200
    assert "Bağlantı yok" in page.text
    assert "csrf_token" not in page.text
    assert "Elif" not in page.text
    worker = client.get("/sw.js").text
    assert '"/offline"' in worker
    assert 'caches.match("/static/panel.css")' not in worker, "the stylesheet as a page"


def test_saving_the_assistant_without_its_speech_model_keeps_the_voice_setting(
    client: TestClient, family_ready: None, db: Database
) -> None:
    with db.session() as session:
        policy = repo.get_or_create_policy(session, 1)
        policy.assistant_enabled = True
        policy.assistant_voice = True
        session.commit()
    login(client)
    page = client.get("/children/1/assistant").text
    assert 'type="hidden" name="assistant_voice" value="on"' in page


def test_the_provision_unit_reads_a_committed_row(
    family_ready: None, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unit is another process: it must find the new name, not the old one."""
    from biokids_core.provision import ProvisionResult

    seen: list[str] = []

    def unit(user: str) -> ProvisionResult:
        with db.session() as other:  # a second connection, like the unit's
            child = repo.get_child_by_user(other, user)
            seen.append(child.display_name if child else "")
        return ProvisionResult(unix_user=user, account_existed=True)

    monkeypatch.setattr(wizard, "provision_via_systemd", unit)
    with db.session() as session:
        child = repo.get_child(session, 1)
        assert child is not None
        child.display_name = "Elif Nur"
        wizard.provision_child(session, child)
    assert seen == ["Elif Nur"]


def test_the_last_wizard_step_offers_nothing_that_was_cut(client: TestClient) -> None:
    """The screen watcher's "coming soon" box went with the watcher (D-180)."""
    client.post("/wizard/1", data={"lang": "tr"})
    page = client.get("/wizard/9").text
    assert 'action="/wizard/9"' in page, "the step itself, not a redirect"
    assert 'id="ai"' not in page


# ------------------------------------------------------- guided first run (D-195)


def test_the_phone_guide_shows_four_steps_and_the_app_stores(
    client: TestClient, family_ready: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from biokids_core import remote

    absent = remote.RemoteStatus(state=remote.RemoteState.ABSENT)
    monkeypatch.setattr(remote, "detect", lambda config=None: absent)
    login(client)
    page = client.get("/settings/phone").text
    assert page.count('class="guide__step') == 4
    assert "apps.apple.com/app/tailscale" in page
    assert "play.google.com/store/apps/details?id=com.tailscale.ipn" in page
    assert "Telefonunuza Tailscale'i kurun" in html.unescape(page)


def test_a_joined_phone_marks_its_step_done(
    client: TestClient, family_ready: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from biokids_core import remote

    serving = remote.RemoteStatus(
        state=remote.RemoteState.SERVING,
        url="https://biokids.tail1a2b3c.ts.net",
        served_ports=(7878,),
        phones=("annes-iphone",),
    )
    monkeypatch.setattr(remote, "detect", lambda config=None: serving)
    login(client)
    page = client.get("/settings/phone").text
    assert "Telefonunuz bağlı: annes-iphone" in page
    assert page.count("is-done") == 3
    assert "https://biokids.tail1a2b3c.ts.net" in page


def test_the_today_screen_asks_for_the_one_restart_setup_needs(
    client: TestClient, family_ready: None, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from biokids_panel.routes import today

    with db.session() as session:
        repo.set_setting(session, wizard.SETTING_RESTART_BOOT, "boot-a")
        session.commit()
    login(client)

    monkeypatch.setattr(today, "read_boot_id", lambda: "boot-a")
    assert 'action="/system/reboot"' in client.get("/today").text
    monkeypatch.setattr(today, "read_boot_id", lambda: "boot-b")
    assert 'action="/system/reboot"' not in client.get("/today").text, "restarted since"


def test_the_restart_needs_its_confirmation(
    client: TestClient, family_ready: None, daemon: FakeDaemon
) -> None:
    login(client)
    response = client.post(
        "/system/reboot", data={"csrf_token": csrf_of(client)}, follow_redirects=False
    )
    assert response.headers["location"] == "/today"
    assert not daemon.ran("reboot_now")

    response = client.post(
        "/system/reboot",
        data={"csrf_token": csrf_of(client), "confirm": "yes"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/today?rebooting=1"
    assert daemon.ran("reboot_now")


def test_finishing_setup_remembers_the_boot_it_finished_in(
    client: TestClient, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wizard, "read_boot_id", lambda: "boot-of-setup")
    client.post("/wizard/1", data={"lang": "tr"})
    client.post("/wizard/2", data={"name": "Anne", "pin": PIN, "pin_again": PIN})
    page = client.get("/wizard/3").text
    marker = 'name="recovery" value="'
    start = page.index(marker) + len(marker)
    recovery = page[start : page.index('"', start)]
    client.post(
        "/wizard/3", data={"passphrase": PASSPHRASE, "recovery": recovery, "written_down": "on"}
    )
    done = client.post("/wizard/9", data={"voices": "on"}, follow_redirects=False)
    assert done.headers["location"] == "/wizard/10"
    closing = client.get("/wizard/10").text
    assert "Bilgisayarı bir kez yeniden başlatın" in closing
    assert 'href="/help"' in closing
    with db.session() as session:
        assert repo.get_setting(session, wizard.SETTING_RESTART_BOOT) == "boot-of-setup"


def test_the_help_page_is_open_before_signing_in(
    client: TestClient, family_ready: None
) -> None:
    """ "I forgot my PIN" is one of its questions."""
    page = client.get("/help")
    assert page.status_code == 200
    assert "PIN'imi unuttum." in html.unescape(page.text)
    assert "sudo biokids-admin reset-pin" in page.text
    assert 'href="/help"' in client.get("/login").text
