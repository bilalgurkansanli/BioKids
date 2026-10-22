"""T9 — the Phase 0 exit criterion, as a test.

`docs/ROADMAP.md` states it as a scenario:

    On a fresh Debian 13 VM: apt install biokids → wizard → phone reaches the
    panel over Tailscale → set a 20-minute quota → log in as the child → the
    ring counts down → warnings at 10/5/1 spoken in Turkish → the machine shuts
    down → the parent's audit log shows every event. Reboot; the counter resumes
    correctly. Change the system clock forward 3 hours; nothing changes.

This runs all of that in one process, in milliseconds, with the real wizard, the
real policy engine, the real ledger, the real daemon loop, the real launcher and
the real IPC handlers. Only three things are faked, and each is a thing a test
machine genuinely does not have: logind, `systemctl`, and a unix socket on the
Windows host this is developed from. The framing, the dispatch and every handler
on both sockets are the production ones.

It does not replace running the criterion on a VM — a passing test here says the
pieces agree with each other, not that Chromium starts. T9 still ends with a
screen recording.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from fastapi.testclient import TestClient

from biokids_core import repo
from biokids_core.clock import FakeClock
from biokids_core.config import Config
from biokids_core.db import AuditLog, Database
from biokids_core.ipc import Op, Request, Response, ShellOp
from biokids_core.types import EndAction, ShutdownOutcome
from biokids_daemon.bus import EventBus
from biokids_daemon.enforcer import Enforcer, RecordingRunner
from biokids_daemon.server import DaemonServer
from biokids_daemon.sessions import FakeSessionSource, SessionWatcher
from biokids_daemon.shell_server import ShellCaller, ShellServer
from biokids_daemon.supervisor import Supervisor
from biokids_panel.app import create_app as create_panel
from biokids_shell.app import create_app as create_shell

PIN = "284913"
PASSPHRASE = "acik kapi mavi deniz"
TICK = 10
QUOTA_MINUTES = 20


class InProcessDaemonClient:
    """The panel's daemon client, wired straight to the real request handler.

    Everything between `Request.encode()` and the handler is production code; the
    only thing missing is the socket, which the Windows development host does
    not have. That is the right seam: a bug in dispatch, in framing or in a
    handler still fails this test.
    """

    def __init__(self, server: DaemonServer) -> None:
        self.server = server
        self._portal: Any = None

    def call(self, op: Any, **args: Any) -> Response:
        from anyio.from_thread import start_blocking_portal

        request = Request(op=str(op), id="acceptance", args=args)
        with start_blocking_portal() as portal:
            response, _ = portal.call(self.server._dispatch, request.encode())
        return response

    def available(self) -> bool:
        return self.call(Op.PING).ok


class InProcessShellClient:
    """The launcher's client, wired to the real shell-socket handlers.

    The caller is constructed directly rather than read from `SO_PEERCRED`, which
    is the one thing this cannot exercise off Linux. Everything the caller is
    then allowed to do is decided by the production handler table.
    """

    def __init__(self, server: ShellServer, child_id: int) -> None:
        self.server = server
        self.child_id = child_id

    def _caller(self) -> ShellCaller:
        runtime = self.server.supervisor.children[self.child_id]
        return ShellCaller(uid=1000, runtime=runtime)

    async def _call(self, op: ShellOp, **args: Any) -> Response:
        request = Request(op=str(op), id="acceptance", args=args)
        response, _ = await self.server._dispatch(self._caller(), request.encode())
        return response

    async def available(self) -> bool:
        return (await self._call(ShellOp.PING)).ok

    async def state(self) -> dict[str, Any] | None:
        response = await self._call(ShellOp.GET_STATE)
        return response.data if response.ok else None

    async def may_launch(self, app_slug: str) -> tuple[bool, str]:
        response = await self._call(ShellOp.LAUNCH, app_slug=app_slug)
        return bool(response.data.get("allow")), str(response.data.get("reason_code", ""))

    async def report_activity(self, app_slug: str, app_name: str, category: str) -> None:
        await self._call(
            ShellOp.REPORT_ACTIVITY, app_slug=app_slug, app_name=app_name, category=category
        )

    async def first_run_done(self) -> None:
        await self._call(ShellOp.FIRST_RUN_DONE)

    async def request_shutdown(self) -> ShutdownOutcome | None:
        response = await self._call(ShellOp.REQUEST_SHUTDOWN)
        return ShutdownOutcome(response.data["outcome"]) if response.ok else None

    async def switch_user(self) -> bool:
        return (await self._call(ShellOp.SWITCH_USER)).ok

    async def ask_a_parent(self, url: str, kind: str = "site") -> tuple[bool, str]:
        response = await self._call(ShellOp.ASK_A_PARENT, url=url, kind=kind)
        return response.ok, response.error or ""


class Machine:
    """One simulated computer: a daemon, a panel, a launcher and a clock."""

    def __init__(self, db: Database, config: Config, clock: FakeClock) -> None:
        self.db = db
        self.config = config
        self.clock = clock
        self.runner = RecordingRunner()
        self.source = FakeSessionSource()
        self.bus = EventBus(queue_size=4096, history_size=4096)
        self.supervisor = Supervisor(
            database=db,
            bus=self.bus,
            watcher=SessionWatcher(self.source),
            enforcer=Enforcer(config, self.runner),
            clock=clock,
            config=config,
        )
        self.server = DaemonServer(self.supervisor, self.bus, config)
        self.shell_server = ShellServer(self.supervisor, self.bus, config)

        self.panel = TestClient(create_panel(config))
        self.panel.app.state.ctx.client = InProcessDaemonClient(self.server)  # type: ignore[attr-defined]

    async def boot(self) -> None:
        await self.supervisor.start()

    def attach_launcher(self, child_id: int) -> TestClient:
        app = create_shell(self.config)
        app.state.shell.client = InProcessShellClient(self.shell_server, child_id)  # type: ignore[assignment]
        app.state.shell.launcher = _NeverActuallyStarts(self.config)
        return TestClient(app, base_url="http://127.0.0.1:7879")

    async def run_for(self, seconds: int) -> None:
        for _ in range(seconds // TICK):
            self.clock.advance(TICK)
            await self.supervisor.tick()

    def spoken(self) -> list[str]:
        return [
            str(event.payload.get("event", ""))
            for event in self.bus.history
            if event.kind == "speak"
        ]

    def audit_actions(self) -> list[str]:
        self.supervisor.session.commit()
        with self.db.session() as session:
            return [row.action for row in session.query(AuditLog).order_by(AuditLog.id)]


class _NeverActuallyStarts:
    """A launcher that records instead of spawning. Nothing here runs Chromium."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.requests: list[Any] = []

    async def launch(self, request: Any) -> Any:
        from biokids_shell.launcher import LaunchResult

        self.requests.append(request)
        return LaunchResult(ok=True, unit=f"biokids-app-{request.slug}-1")


@pytest.fixture
def clock() -> FakeClock:
    """A Monday afternoon, so the weekday quota applies and no window closes."""
    return FakeClock(wall=dt.datetime(2026, 9, 21, 16, 0, 0))


@pytest.fixture
async def machine(db: Database, config: Config, clock: FakeClock) -> Machine:
    return Machine(db, config, clock)


def complete_the_wizard(panel: TestClient, quota_minutes: int) -> None:
    """Exactly what a parent does, through the real forms."""
    this_year = dt.date.today().year
    panel.post("/wizard/1", data={"lang": "tr"})
    panel.post("/wizard/2", data={"name": "Anne", "pin": PIN, "pin_again": PIN})

    page = panel.get("/wizard/3").text
    marker = 'name="recovery" value="'
    start = page.index(marker) + len(marker)
    recovery = page[start : page.index('"', start)]
    panel.post(
        "/wizard/3",
        data={"passphrase": PASSPHRASE, "recovery": recovery, "written_down": "on"},
    )
    panel.post(
        "/wizard/4",
        data={
            "display_name": "Elif",
            "birth_year": this_year - 6,
            "unix_user": "elif",
            "avatar": "fox",
            "locale": "tr",
        },
    )
    panel.post("/wizard/5", data={"apps": ["tuxpaint", "eba"]})
    panel.post(
        "/wizard/6",
        data={
            "weekday_quota_min": quota_minutes,
            "weekend_quota_min": quota_minutes,
            "window": "",
            "end_action": EndAction.POWEROFF.value,
        },
    )
    panel.post("/wizard/7", data={"mode": "allowlist"})
    panel.post("/wizard/8", data={"enable": ""})
    panel.post("/wizard/9", data={"voices": "on"})


# --------------------------------------------------------------- the criterion


async def test_the_phase_0_exit_criterion(machine: Machine, clock: FakeClock) -> None:
    """The whole scenario, in order, as one story."""
    panel = machine.panel

    # --- a fresh install offers the wizard and nothing else -----------------
    assert panel.get("/", follow_redirects=False).headers["location"] == "/wizard/1"
    complete_the_wizard(panel, QUOTA_MINUTES)

    # --- the parent signs in with their PIN ---------------------------------
    assert panel.post("/login", data={"pin": PIN}, follow_redirects=False).status_code == 303
    await machine.boot()
    machine.supervisor.reload()
    child_id = next(iter(machine.supervisor.children))

    status = panel.get("/today")
    assert status.status_code == 200
    assert "Elif" in status.text

    # --- the child logs in and the launcher comes up ------------------------
    machine.source.log_in("elif")
    await machine.supervisor.watcher.refresh()
    launcher = machine.attach_launcher(child_id)

    first_run = launcher.get("/", follow_redirects=False)
    assert first_run.headers["location"] == "/first-run", "the explanation runs first (D-111)"
    launcher.post("/first-run", follow_redirects=False)

    home = launcher.get("/").text
    assert "Merhaba Elif" in home
    assert "Boya" in home, "only the apps the parent enabled"

    # --- the ring counts down -----------------------------------------------
    await machine.run_for(60)
    state = launcher.get("/state").json()
    assert state["remaining_seconds"] == QUOTA_MINUTES * 60 - 50

    # --- moving the clock forward three hours changes nothing ---------------
    before = machine.supervisor.ledger.state(child_id).general_used_s
    clock.jump_wall(3 * 3600)
    await machine.supervisor.tick()
    assert machine.supervisor.ledger.state(child_id).general_used_s == before
    assert "clock_jump" in machine.audit_actions()

    # --- warnings at ten, five and one minute, in Turkish --------------------
    # The jump left the clock after seven in the evening, so this day ends at
    # night: the same warnings at the same minutes, in the night's words
    # (D-214). The daytime ones are test_daemon's, at four in the afternoon.
    await machine.run_for(QUOTA_MINUTES * 60)
    warnings = [e for e in machine.spoken() if e.startswith(("warn_", "bedtime_"))]
    assert warnings == ["bedtime_10", "bedtime_5", "bedtime_1"]

    from biokids_core.i18n import translator

    assert translator("tr")("quota.warning", minutes=10) == "10 dakikan kaldı"

    # --- zero: the screen locks, and the child's launcher says so -----------
    assert machine.runner.ran("loginctl", "lock-session")
    assert not machine.runner.ran("systemctl", "poweroff"), "not during the countdown"
    assert "İyi geceler, Elif." in launcher.get("/").text

    # --- then the machine shuts down ----------------------------------------
    await machine.run_for(150)
    assert machine.runner.ran("systemctl", "poweroff")

    calls = [" ".join(call) for call in machine.runner.calls]
    stopped_at = next(i for i, call in enumerate(calls) if "stop" in call)
    powered_at = next(i for i, call in enumerate(calls) if "poweroff" in call)
    assert stopped_at < powered_at, "applications get their SIGTERM first"

    # --- the audit log shows every event ------------------------------------
    actions = machine.audit_actions()
    for expected in (
        "setup_completed",
        "child_added",
        "login_ok",
        "daemon_started",
        "warning_spoken",
        "quota_exhausted",
        "clock_jump",
        "poweroff",
    ):
        assert expected in actions, f"{expected} is missing from the audit log"


async def test_the_counter_resumes_correctly_after_a_reboot(
    db: Database, config: Config, clock: FakeClock
) -> None:
    """Turn it off and on again; the day's usage is still the day's usage."""
    first = Machine(db, config, clock)
    complete_the_wizard(first.panel, QUOTA_MINUTES)
    await first.boot()
    first.supervisor.reload()
    child_id = next(iter(first.supervisor.children))

    first.source.log_in("elif")
    await first.run_for(8 * 60)
    used = first.supervisor.ledger.state(child_id).general_used_s
    await first.supervisor.stop()
    assert used == 8 * 60 - TICK

    clock.reboot(off_for=5 * 60)

    second = Machine(db, config, clock)
    await second.boot()
    second.supervisor.reload()
    second.source.log_in("elif")

    assert second.supervisor.ledger.state(child_id).general_used_s == used
    await second.run_for(2 * 60)
    assert second.supervisor.ledger.state(child_id).general_used_s == used + 2 * 60 - TICK

    remaining = second.supervisor.status()["children"][0]["remaining_seconds"]
    assert remaining == QUOTA_MINUTES * 60 - (used + 2 * 60 - TICK)


async def test_a_parent_can_grant_time_from_the_panel_while_the_child_waits(
    machine: Machine, clock: FakeClock
) -> None:
    """The "+10 dk" button, through the real panel form and the real socket op."""
    panel = machine.panel
    complete_the_wizard(panel, 2)
    panel.post("/login", data={"pin": PIN})
    await machine.boot()
    machine.supervisor.reload()
    child_id = next(iter(machine.supervisor.children))

    machine.source.log_in("elif")
    await machine.run_for(3 * 60)
    assert machine.runner.ran("loginctl", "lock-session")

    page = panel.get("/today").text
    marker = 'name="csrf_token" value="'
    start = page.index(marker) + len(marker)
    token = page[start : page.index('"', start)]

    response = panel.post(
        f"/children/{child_id}/grant",
        data={"minutes": 10, "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303

    await machine.run_for(60)
    assert not machine.runner.ran("systemctl", "poweroff"), "the countdown was called off"
    assert machine.supervisor.status()["children"][0]["remaining_seconds"] > 0
    assert "time_granted" in machine.audit_actions()


async def test_a_child_cannot_reach_another_childs_state(
    machine: Machine, clock: FakeClock
) -> None:
    """The shell socket answers about the caller, and takes no child id at all."""
    panel = machine.panel
    complete_the_wizard(panel, QUOTA_MINUTES)
    panel.post("/login", data={"pin": PIN})
    await machine.boot()

    page = panel.get("/today").text
    marker = 'name="csrf_token" value="'
    start = page.index(marker) + len(marker)
    token = page[start : page.index('"', start)]
    panel.post(
        "/children",
        data={
            "display_name": "Kerem",
            "birth_year": dt.date.today().year - 9,
            "unix_user": "kerem",
            "avatar": "owl",
            "locale": "tr",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    machine.supervisor.reload()

    with machine.db.session() as session:
        elif_id = repo.get_child_by_user(session, "elif").id  # type: ignore[union-attr]
        kerem_id = repo.get_child_by_user(session, "kerem").id  # type: ignore[union-attr]

    client = InProcessShellClient(machine.shell_server, elif_id)

    # Asking for the other child by name changes nothing: the handler never
    # looks at an argument for identity.
    response = await client._call(ShellOp.GET_STATE, child_id=kerem_id)
    assert response.data["child_id"] == elif_id
    assert response.data["display_name"] == "Elif"


async def test_a_child_cannot_grant_themselves_time(machine: Machine, clock: FakeClock) -> None:
    """The privileged operations are simply not on the launcher's socket."""
    complete_the_wizard(machine.panel, QUOTA_MINUTES)
    await machine.boot()
    machine.supervisor.reload()
    child_id = next(iter(machine.supervisor.children))
    client = InProcessShellClient(machine.shell_server, child_id)

    for forbidden in (Op.GRANT_TIME, Op.SET_POLICY, Op.POWEROFF_NOW, Op.RELOAD):
        response = await client._call(forbidden, child_id=child_id, seconds=9999)  # type: ignore[arg-type]
        assert response.ok is False, f"{forbidden} must not be reachable from the child"
        assert response.error == "shell.not_allowed"

    assert machine.supervisor.ledger.state(child_id).granted_s == 0
