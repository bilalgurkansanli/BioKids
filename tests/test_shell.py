"""T5 — the child launcher.

What is worth asserting about a launcher is what a seven-year-old can and
cannot make it do. So: that the tile grid only ever shows what a parent
enabled, that a refused launch lands on a calm screen with a way to ask a
grown-up rather than an error, that every full-screen state is reachable and
none of them can be clicked away when it should not be, and that a launcher
whose daemon has died shows a holding screen instead of an unsupervised
computer.

The daemon is faked at the client boundary. Nothing here needs a socket, a
browser or a sound card.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import shutil
from typing import Any

import pytest
from fastapi.testclient import TestClient

from biokids_core.config import Config
from biokids_core.types import AgeBand, Reason
from biokids_shell import app as shell_app
from biokids_shell import launcher as launcher_module
from biokids_shell.app import create_app
from biokids_shell.kiosk import (
    MANAGED_POLICY_DIR,
    POLICY_FILENAME,
    KioskSupervisor,
    kiosk_policy,
)
from biokids_shell.launcher import AppLauncher, LaunchRequest


def a_state(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "child_id": 1,
        "display_name": "Elif",
        "avatar": "fox",
        "locale": "tr",
        "age_band": AgeBand.YOUNG.value,
        "managed": True,
        "supervision_indicator": "badge",
        "first_run_done": True,
        "tiles": [
            {
                "slug": "tuxpaint",
                "name": "Boya",
                "icon": "brush",
                "kind": "app",
                "url": "",
                "exec": "tuxpaint",
                "category": "free",
            },
            {
                "slug": "eba",
                "name": "EBA",
                "icon": "school",
                "kind": "web",
                "url": "https://www.eba.gov.tr",
                "exec": "",
                "category": "learning",
            },
        ],
        "remaining_seconds": 42 * 60,
        "remaining_minutes": 42,
        "quota_seconds": 60 * 60,
        "used_seconds": 18 * 60,
        "earned_seconds": 0,
        "break_seconds": 0,
        "penalty_seconds": 0,
        "allow": True,
        "action": "none",
        "reason_code": Reason.ALLOWED,
        "idle": False,
        "countdown_seconds": 0,
    }
    base.update(overrides)
    return base


class FakeShellDaemon:
    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self.data = state
        self.launch_answer: tuple[bool, str] = (True, Reason.ALLOWED)
        self.launched: list[str] = []
        self.activity: list[tuple[str, str, str]] = []
        self.first_run_calls = 0
        self.shutdown_calls = 0
        self.asked: list[str] = []
        self.shutdown_ok = True

    async def available(self) -> bool:
        return self.data is not None

    async def state(self) -> dict[str, Any] | None:
        return self.data

    async def may_launch(self, app_slug: str) -> tuple[bool, str]:
        self.launched.append(app_slug)
        return self.launch_answer

    async def report_activity(self, app_slug: str, app_name: str, category: str) -> None:
        self.activity.append((app_slug, app_name, category))

    async def first_run_done(self) -> None:
        self.first_run_calls += 1
        if self.data:
            self.data["first_run_done"] = True

    async def request_shutdown(self) -> bool:
        self.shutdown_calls += 1
        return self.shutdown_ok

    async def ask_a_parent(self, url: str) -> bool:
        self.asked.append(url)
        return True


def _visible_text(page: str) -> str:
    """Everything between the tags: what a child actually reads."""
    out: list[str] = []
    depth = 0
    for char in page:
        if char == "<":
            depth += 1
        elif char == ">":
            depth -= 1
        elif depth == 0:
            out.append(char)
    return "".join(out)


@pytest.fixture
def daemon() -> FakeShellDaemon:
    return FakeShellDaemon(a_state())


@pytest.fixture
def client(config: Config, daemon: FakeShellDaemon) -> TestClient:
    app = create_app(config)
    app.state.shell.client = daemon  # type: ignore[assignment]
    # Never actually start a process during a test.
    app.state.shell.launcher = _RecordingLauncher(config)
    return TestClient(app)


class _RecordingLauncher(AppLauncher):
    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self.requests: list[LaunchRequest] = []

    async def launch(self, request: LaunchRequest) -> Any:
        from biokids_shell.launcher import LaunchResult

        self.requests.append(request)
        return LaunchResult(ok=True, unit=f"biokids-app-{request.slug}-1")


# ------------------------------------------------------------------ the grid


def test_the_home_screen_shows_the_enabled_tiles(client: TestClient) -> None:
    page = client.get("/").text
    assert "Merhaba Elif" in page
    assert "Boya" in page
    assert "EBA" in page


def test_the_grid_shows_nothing_a_parent_did_not_enable(
    client: TestClient, daemon: FakeShellDaemon
) -> None:
    assert daemon.data is not None
    daemon.data["tiles"] = []
    page = client.get("/").text
    assert "Boya" not in page
    assert "hiç uygulaman yok" in page


def test_the_young_band_gets_no_clock_and_no_number_on_the_ring(
    client: TestClient, daemon: FakeShellDaemon
) -> None:
    """4–7 sees a shrinking circle, not a number they cannot read (UX.md §2.2)."""
    page = client.get("/").text
    assert 'id="clock"' not in page
    assert 'id="ring-label"' not in page


def test_the_older_band_gets_both(client: TestClient, daemon: FakeShellDaemon) -> None:
    assert daemon.data is not None
    daemon.data["age_band"] = AgeBand.OLDER.value
    page = client.get("/").text
    assert 'id="clock"' in page
    assert 'id="ring-label"' in page


def test_the_ring_is_drawn_from_the_daemons_numbers(client: TestClient) -> None:
    """42 of 60 minutes left is 70%, so 30% of the circumference is offset."""
    page = client.get("/").text
    assert 'stroke-dashoffset="75.4"' in page
    assert 'data-tone="ok"' in page


def test_the_ring_turns_red_and_thick_near_the_end(
    client: TestClient, daemon: FakeShellDaemon
) -> None:
    assert daemon.data is not None
    daemon.data["remaining_seconds"] = 4 * 60
    assert 'data-tone="danger"' in client.get("/").text


# ---------------------------------------------------------------- launching


def test_tapping_a_tile_paints_before_it_asks(
    client: TestClient, daemon: FakeShellDaemon
) -> None:
    """The loading screen shows the app's own icon and renders without asking."""
    page = client.get("/launch/tuxpaint").text
    assert "Boya" in page
    assert "🖌️" in page
    assert daemon.launched == [], "the GET must not have asked the daemon yet"


def test_launching_asks_the_daemon_and_reports_the_activity(
    client: TestClient, daemon: FakeShellDaemon
) -> None:
    response = client.post("/launch/tuxpaint")
    assert response.json()["ok"] is True
    assert daemon.launched == ["tuxpaint"]
    assert daemon.activity == [("tuxpaint", "Boya", "free")]


def test_a_refused_launch_says_why_without_starting_anything(
    client: TestClient, daemon: FakeShellDaemon
) -> None:
    daemon.launch_answer = (False, Reason.QUOTA_EXHAUSTED)
    body = client.post("/launch/tuxpaint").json()
    assert body["ok"] is False
    assert body["reason"] == Reason.QUOTA_EXHAUSTED
    assert client.app.state.shell.launcher.requests == []  # type: ignore[attr-defined]


def test_a_tile_that_is_not_on_this_childs_list_is_refused(client: TestClient) -> None:
    body = client.post("/launch/doom").json()
    assert body["ok"] is False
    assert body["reason"] == Reason.APP_NOT_ALLOWED


def test_a_builtin_tile_starts_no_process(client: TestClient, daemon: FakeShellDaemon) -> None:
    assert daemon.data is not None
    daemon.data["tiles"].append(
        {
            "slug": "volume",
            "name": "Ses",
            "icon": "speaker",
            "kind": "builtin",
            "category": "free",
        }
    )
    body = client.post("/launch/volume").json()
    assert body["ok"] is True
    assert body["builtin"] == "volume"
    assert daemon.launched == [], "a slider does not need permission"


# ------------------------------------------------------------- the screens


@pytest.mark.parametrize(
    ("reason", "marker"),
    [
        (Reason.QUOTA_EXHAUSTED, "Bugünlük bu kadar"),
        (Reason.WINDOW_CLOSED, "bilgisayar saati değil"),
        (Reason.BREAK_ACTIVE, "Biraz mola"),
        (Reason.PENALTY_ACTIVE, "ara veriyoruz"),
    ],
)
def test_every_blocked_state_has_its_own_screen(
    client: TestClient, daemon: FakeShellDaemon, reason: str, marker: str
) -> None:
    assert daemon.data is not None
    daemon.data["allow"] = False
    daemon.data["reason_code"] = reason
    daemon.data["break_seconds"] = 300
    daemon.data["penalty_seconds"] = 300
    page = client.get("/").text
    assert marker in page


def test_the_break_screen_has_no_way_out(client: TestClient, daemon: FakeShellDaemon) -> None:
    """A break you can click away is not a break (D-46)."""
    assert daemon.data is not None
    daemon.data["allow"] = False
    daemon.data["reason_code"] = Reason.BREAK_ACTIVE
    daemon.data["break_seconds"] = 300
    page = client.get("/").text
    assert "<form" not in page
    assert 'href="/"' not in page


def test_the_block_screen_never_shows_a_url_or_an_error_code(client: TestClient) -> None:
    """UX.md §2.5: never a URL, never an error code, never the word "forbidden".

    The URL is in the page — in a hidden field, because "Anneme sor" has to tell
    the parent *which* site — but nothing the child can read contains it. That
    distinction is the whole rule: the child sees a sentence, the parent sees an
    address.
    """
    page = client.get("/blocked?url=https://example.com/bad&reason=filter.blocked").text
    visible = _visible_text(page)

    assert "example.com" not in visible
    assert "filter.blocked" not in visible
    assert "http" not in visible
    # Parent-neutral on purpose: BioKids has one parent account, and naming
    # the mother sends a child whose account holder is their father looking for
    # someone who is not there.
    assert "Büyüğüme sor" in visible
    assert "Anneme" not in visible and "Babama" not in visible
    assert 'value="https://example.com/bad"' in page, "but the request still carries it"


def test_asking_a_parent_files_the_request(client: TestClient, daemon: FakeShellDaemon) -> None:
    response = client.post("/ask-a-parent", data={"url": "https://example.com/bad"})
    assert response.status_code == 200
    assert daemon.asked == ["https://example.com/bad"]
    assert "Sorduk" in response.text


def test_the_first_run_screen_runs_before_anything_else(
    client: TestClient, daemon: FakeShellDaemon
) -> None:
    """Non-skippable on first login (D-111)."""
    assert daemon.data is not None
    daemon.data["first_run_done"] = False

    redirect = client.get("/", follow_redirects=False)
    assert redirect.status_code == 303
    assert redirect.headers["location"] == "/first-run"

    page = client.get("/first-run").text
    assert "ben Zio" in page
    assert "sır değil" in page, "the child is told the supervision is not a secret"

    client.post("/first-run", follow_redirects=False)
    assert daemon.first_run_calls == 1


def test_the_child_can_shut_the_machine_down_but_is_asked_first(
    client: TestClient, daemon: FakeShellDaemon
) -> None:
    confirm = client.get("/shutdown").text
    assert "kapatalım mı" in confirm
    assert daemon.shutdown_calls == 0

    done = client.post("/shutdown")
    assert daemon.shutdown_calls == 1
    assert "Yarın görüşürüz" in done.text


def test_a_refused_shutdown_returns_to_the_launcher(
    client: TestClient, daemon: FakeShellDaemon
) -> None:
    daemon.shutdown_ok = False
    page = client.post("/shutdown").text
    assert "Merhaba Elif" in page


def test_the_launcher_holds_when_the_daemon_is_gone(config: Config) -> None:
    """A working launcher with no limits behind it is the failure to avoid."""
    app = create_app(config)
    app.state.shell.client = FakeShellDaemon(None)  # type: ignore[assignment]
    client = TestClient(app)

    page = client.get("/").text
    assert "Boya" not in page, "no application may be launchable with nothing supervising"
    assert "cevap vermiyor" in page, "the child is told why there is nothing here"


def test_the_launcher_does_not_address_a_six_year_old_as_siz(config: Config) -> None:
    """This screen used to borrow the panel's sentence, and the panel means a parent.

    That sentence is formal - correct for a parent - and it tells the reader to
    restart the machine, which a six-year-old can neither do nor should. The
    shared string stays as it is; the child's screen gets its own words.
    """
    app = create_app(config)
    app.state.shell.client = FakeShellDaemon(None)  # type: ignore[assignment]
    page = TestClient(app).get("/").text

    assert "deneyin" not in page, "the child is addressed as 'siz'"
    assert "yeniden başlat" not in page, "advice only a parent can act on"
    assert "büyüğüne" in page, "a child with nothing working needs a person, not a command"


def test_the_time_up_screen_says_what_will_actually_happen(
    config: Config, daemon: FakeShellDaemon
) -> None:
    """Three end actions (D-43), and the screen used to announce the same one.

    A child told to save because the computer is about to shut down, whose
    screen then merely locks, has learned that the warning is not true - which
    is the one thing this screen cannot afford, because the next time it says it
    the machine really will power off.
    """
    assert daemon.data is not None
    wording = {}
    for action in ("poweroff", "lock", "logout"):
        daemon.data.update(
            allow=False,
            reason_code="quota.exhausted",
            end_action=action,
            countdown_seconds=90,
        )
        app = create_app(config)
        app.state.shell.client = daemon  # type: ignore[assignment]
        wording[action] = TestClient(app).get("/").text

    assert "kapanacak" in wording["poweroff"]
    assert "kilitlenecek" in wording["lock"], "a lock was announced as a shutdown"
    assert "kapanacak" not in wording["lock"]
    assert "Oturumun" in wording["logout"]


def test_an_unknown_end_action_falls_back_rather_than_guessing(
    config: Config, daemon: FakeShellDaemon
) -> None:
    """A daemon that sent no end action must not make the screen claim one."""
    assert daemon.data is not None
    daemon.data.update(
        allow=False, reason_code="quota.exhausted", end_action="", countdown_seconds=90
    )
    app = create_app(config)
    app.state.shell.client = daemon  # type: ignore[assignment]
    page = TestClient(app).get("/").text

    assert "süren doldu" in page
    assert "kapanacak" not in page and "kilitlenecek" not in page


def test_english_renders_for_an_english_child(
    client: TestClient, daemon: FakeShellDaemon
) -> None:
    assert daemon.data is not None
    daemon.data["locale"] = "en"
    assert "Hello Elif" in client.get("/").text


def test_the_page_carries_its_strings_as_data_not_as_javascript(client: TestClient) -> None:
    """Every sentence stays in the catalogs (ARCHITECTURE.md §8)."""
    page = client.get("/").text
    assert 'id="strings" type="application/json"' in page
    assert "dakikan kald" in page


# ------------------------------------------------------------------- kiosk


def test_the_kiosk_policy_allows_exactly_one_origin() -> None:
    policy = kiosk_policy("127.0.0.1:7879")
    assert policy["URLBlocklist"] == ["*"]
    assert policy["URLAllowlist"] == ["127.0.0.1:7879"]
    assert policy["URLBlocklistExceptions"] == []


def test_the_policy_lets_the_childs_web_tiles_through() -> None:
    """The file is machine-wide, so it governs the tile's window as well (D-156).

    With only the launcher allowed, every web tile opened on "This page is
    blocked". Hosts arrive without `www.` so their subdomains are covered too.
    """
    policy = kiosk_policy("127.0.0.1:7879", ["trtcocuk.net.tr", "eba.gov.tr", "eba.gov.tr"])

    assert policy["URLAllowlist"] == ["127.0.0.1:7879", "eba.gov.tr", "trtcocuk.net.tr"]
    assert policy["URLBlocklist"] == ["*"], "everything else stays closed"


def test_the_childs_browser_keeps_no_passwords_in_a_keyring(config: Config) -> None:
    """Or GNOME's keyring asks the child to invent a password for it (D-156)."""
    argv = AppLauncher(config).browser_argv("https://www.trtcocuk.net.tr")
    assert "--password-store=basic" in argv


def test_the_childs_browser_does_not_offer_to_restore_after_a_shutdown(config: Config) -> None:
    """The end-of-day shutdown is not a crash, and a child cannot answer the question."""
    argv = AppLauncher(config).browser_argv("https://www.trtcocuk.net.tr")
    assert "--hide-crash-restore-bubble" in argv


def test_the_kiosk_policy_closes_the_obvious_ways_out() -> None:
    policy = kiosk_policy("127.0.0.1:7879")
    assert policy["DeveloperToolsAvailability"] == 2
    assert policy["IncognitoModeAvailability"] == 1
    assert policy["ExtensionInstallBlocklist"] == ["*"]
    assert policy["DownloadRestrictions"] == 3
    assert policy["PrintingEnabled"] is False
    assert policy["DnsOverHttpsMode"] == "off"


def test_the_kiosk_policy_is_written_atomically(config: Config, tmp_path: Any) -> None:
    """A half-written policy is one Chromium ignores entirely."""
    supervisor = KioskSupervisor(
        config, profile_dir=tmp_path / "kiosk", _policy_dir=tmp_path / "managed"
    )
    path = supervisor.write_policy()
    assert path is not None and path.is_file()
    assert not list(path.parent.glob("*.tmp")), "no temporary file is left behind"
    assert '"URLBlocklist"' in path.read_text(encoding="utf-8")


def test_the_policy_goes_where_chromium_actually_reads_it(config: Config) -> None:
    """Not `<user-data-dir>/policies/managed/`, which is where it used to go.

    Chromium's Linux policy loader reads only /etc/chromium/policies/{managed,
    recommended}; `--user-data-dir` adds no policy source. So the file was
    written, the docstring said Chromium reads it, and `chrome://policy` in the
    kiosk profile was empty - URLBlocklist ["*"] never applied, DevTools
    available, downloads unrestricted. CLAUDE.md section 3 and
    ARCHITECTURE.md already named the fixed path; only the code disagreed.
    """
    supervisor = KioskSupervisor(config)
    assert supervisor.policy_dir == config.paths.chromium_policy_dir
    # The default is the real path. A test run only moves it because
    # BIOKIDS_PREFIX moves every path, which is what makes the suite
    # runnable on a machine that has no /etc/chromium.
    assert str(MANAGED_POLICY_DIR).replace("\\", "/") == "/etc/chromium/policies/managed"
    assert (
        str(supervisor.policy_dir).replace("\\", "/").endswith("/etc/chromium/policies/managed")
    )
    assert "user-data-dir" not in str(supervisor.policy_dir)


def test_a_kiosk_with_no_policy_is_not_reported_as_locked_down(
    config: Config, tmp_path: Any
) -> None:
    """The launcher runs as the child and cannot write /etc.

    On a packaged machine the file is already there. When it is not, returning a
    path would tell the caller the kiosk is locked down when it is a browser
    with no limits - so there is no path to return.
    """
    unwritable = tmp_path / "nope"
    unwritable.write_text("not a directory", encoding="utf-8")
    supervisor = KioskSupervisor(config, _policy_dir=unwritable / "managed")

    assert supervisor.policy_in_force() is False
    assert supervisor.write_policy() is None


def test_a_policy_the_package_already_placed_is_accepted(config: Config, tmp_path: Any) -> None:
    """The normal path on a real machine: root put it there, the child reads it."""
    managed = tmp_path / "managed"
    KioskSupervisor(config, _policy_dir=managed).write_policy()

    child_view = KioskSupervisor(config, _policy_dir=managed)
    assert child_view.policy_in_force() is True


def test_a_policy_for_a_different_origin_does_not_count(config: Config, tmp_path: Any) -> None:
    """A stale file from a changed port is not the policy this kiosk needs."""
    managed = tmp_path / "managed"
    managed.mkdir(parents=True)
    (managed / POLICY_FILENAME).write_text(
        json.dumps(kiosk_policy("127.0.0.1:9999")), encoding="utf-8"
    )

    assert KioskSupervisor(config, _policy_dir=managed).policy_in_force() is False


def test_the_kiosk_is_pointed_at_the_launcher_and_nothing_else(config: Config) -> None:
    argv = KioskSupervisor(config).argv()
    assert "--kiosk" in argv
    assert f"--app=http://{config.shell.host}:{config.shell.port}/" in argv
    assert any(part.startswith("--user-data-dir=") for part in argv)


# ---------------------------------------------------------------- launching


def test_a_web_tile_opens_the_childs_own_browser_profile(config: Config) -> None:
    """Not the kiosk profile: that one must stay locked to the launcher."""
    launcher = AppLauncher(config)
    argv = launcher.build_argv(
        LaunchRequest(slug="eba", name="EBA", kind="web", url="https://www.eba.gov.tr")
    )
    assert argv is not None
    assert argv[-1] == "https://www.eba.gov.tr"
    assert any("browser" in part for part in argv)
    assert not any("kiosk" in part for part in argv)


def test_a_tile_with_nothing_to_start_does_not_pretend_to(config: Config) -> None:
    launcher = AppLauncher(config)
    assert launcher.build_argv(LaunchRequest(slug="volume", name="Ses", kind="builtin")) is None
    assert (
        launcher.build_argv(LaunchRequest(slug="x", name="X", kind="app", exec_cmd="")) is None
    )


def test_a_slug_cannot_escape_into_a_systemd_unit_name() -> None:
    from biokids_shell.launcher import _safe_unit

    assert _safe_unit("../../etc/passwd") == "------etc-passwd"
    assert _safe_unit("") == "app"
    assert len(_safe_unit("x" * 200)) == 48


# ---------------------------------------------- the packaged session hardening


def test_the_virtual_terminal_guards_are_actually_shipped() -> None:
    """D-54 names two files and T5.7 requires them; neither existed.

    The XFCE README said `biokids-daemon` installed them. Nothing installed
    them, so `Ctrl+Alt+F2` on a real machine gave the child a login prompt while
    a parent reading the documentation was told that escape was handled - which
    is worse than not claiming it, because it is the claim a parent relies on.

    Asserted on the packaging rather than on a running machine: the failure was
    a file that did not exist, and that is checkable anywhere.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    logind = root / "debian/etc/systemd/logind.conf.d/biokids.conf"
    xorg = root / "debian/etc/X11/xorg.conf.d/50-biokids-no-vt-switch.conf"

    assert logind.is_file(), "no getty configuration; Ctrl+Alt+F2 offers a login"
    assert xorg.is_file(), "no DontVTSwitch; the child can leave the session"

    logind_text = logind.read_text(encoding="utf-8")
    assert "NAutoVTs=1" in logind_text, "gettys on VT2-VT6 are a shell for the asking"
    assert "ReserveVT=1" in logind_text, (
        "one VT stays reserved on purpose: a machine a parent cannot recover "
        "from a broken X server is a worse outcome than the escape this closes"
    )

    # Whitespace-insensitive: the file aligns its values, and a test that
    # breaks on alignment teaches people to reformat rather than to think.
    xorg_text = " ".join(xorg.read_text(encoding="utf-8").split())
    assert 'Option "DontVTSwitch" "true"' in xorg_text
    assert 'Option "DontZap" "true"' in xorg_text, "Ctrl+Alt+Backspace kills the launcher"

    shipped = (root / "debian/biokids-shell.install").read_text(encoding="utf-8")
    assert "logind.conf.d" in shipped, "the file exists but no package installs it"
    assert "xorg.conf.d" in shipped, "the file exists but no package installs it"


# ------------------------------------------- launching returns when the app starts


class _FakeProcess:
    """A `systemd-run --scope` that has already exec'd into the application."""

    def __init__(self, *, returncode: int | None = None, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stderr = stderr
        self.waits = 0

    async def wait(self) -> int | None:
        self.waits += 1
        if self.returncode is None:
            # The application is running. This is the call that used to block
            # until the child quit TuxPaint.
            await asyncio.Event().wait()
        return self.returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        """What the old code awaited. Blocks while the application is running,
        which is precisely the defect: it returned when the child closed it."""
        if self.returncode is None:
            await asyncio.Event().wait()
        return b"", self._stderr

    @property
    def stderr(self) -> Any:
        payload = self._stderr

        class _Reader:
            async def read(self) -> bytes:
                return payload

        return _Reader()


async def test_launching_returns_while_the_app_is_still_running(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`systemd-run --scope` execs the app; it does not fork and return.

    So `communicate()` waited for the whole life of the application:
    `report_activity` reached the daemon only after the child closed it, and the
    launch result was the app's own exit status - which meant that when the
    daemon stopped the scope at quota time, the launcher reported the launch had
    failed.
    """
    launcher = AppLauncher(config)
    running = _FakeProcess(returncode=None)

    async def fake_exec(*_a: Any, **_k: Any) -> Any:
        return running

    monkeypatch.setattr(shutil, "which", lambda _n: "/usr/bin/systemd-run")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(launcher_module, "SCOPE_REFUSAL_TIMEOUT_S", 0.05)

    request = LaunchRequest(slug="tuxpaint", name="Boya", kind="app", exec_cmd="tuxpaint")
    result = await asyncio.wait_for(launcher.launch(request), timeout=5)

    assert result.ok is True
    assert result.unit and result.unit.startswith("biokids-app-tuxpaint")
    assert launcher.running["tuxpaint"] == result.unit


async def test_a_refused_scope_is_still_reported_as_a_failure(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not waiting must not mean not noticing. A refusal happens before the exec."""
    launcher = AppLauncher(config)
    refused = _FakeProcess(returncode=1, stderr=b"Failed to start transient scope unit")

    async def fake_exec(*_a: Any, **_k: Any) -> Any:
        return refused

    monkeypatch.setattr(shutil, "which", lambda _n: "/usr/bin/systemd-run")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(launcher_module, "SCOPE_REFUSAL_TIMEOUT_S", 0.05)

    request = LaunchRequest(slug="tuxpaint", name="Boya", kind="app", exec_cmd="tuxpaint")
    result = await asyncio.wait_for(launcher.launch(request), timeout=5)

    assert result.ok is False
    assert "tuxpaint" not in launcher.running, "a refused launch was recorded as running"


def test_the_character_art_is_found_on_an_installed_machine(
    config: Config, tmp_path: Any
) -> None:
    """`CHARACTER_DIR` is two levels above the package.

    In a checkout that is `assets/`; on an installed machine it is
    `/usr/lib/python3`, so `_load_characters()` returned {} without a word in
    the log. The first-run screen introduced "ben Zio" next to nothing and every
    speech card had an empty character slot.
    """
    installed = config.paths.share_dir / "characters"
    installed.mkdir(parents=True, exist_ok=True)
    (installed / "zio.svg").write_text('<svg class="bk-char"/>', encoding="utf-8")
    (installed / "sio.svg").write_text('<svg class="bk-char"/>', encoding="utf-8")

    found = shell_app._load_characters(config)

    assert set(found) == {"zio", "sio"}
    assert installed in shell_app.character_dirs(config)
    assert shell_app.character_dirs(config)[0] == installed, (
        "the installed copy must win over a source checkout"
    )
