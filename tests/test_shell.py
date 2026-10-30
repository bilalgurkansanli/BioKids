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
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from biokids_core.config import Config
from biokids_core.errors import IpcError
from biokids_core.types import AgeBand, Reason, ShutdownOutcome
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

LAUNCHER = "http://127.0.0.1:7879"
"""Where the kiosk opens the launcher; any other Host is refused (DNS rebinding)."""


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
        self.asked_kinds: list[str] = []
        self.visits: list[str] = []
        self.searches: list[tuple[str, str]] = []
        self.english: dict[str, Any] | None = {"today": "2026-10-22", "items": {}}
        self.english_answers: list[tuple[str, bool]] = []
        self.heard: list[str] = []
        self.hear_text: str | None = "a cat"
        self.chat_asked: list[tuple[str, dict[str, Any]]] = []
        self.shutdown_answer: ShutdownOutcome | None = ShutdownOutcome.POWEROFF
        self.switch_answer = True
        self.switch_calls = 0
        self.finish_up_answer = 0
        self.finish_up_calls = 0

    async def available(self) -> bool:
        return self.data is not None

    async def state(self) -> dict[str, Any] | None:
        return self.data

    async def may_launch(self, app_slug: str) -> tuple[bool, str]:
        self.launched.append(app_slug)
        return self.launch_answer

    async def report_activity(self, app_slug: str, app_name: str = "", unit: str = "") -> None:
        self.activity.append((app_slug, app_name, unit))

    async def finish_up(self) -> int:
        self.finish_up_calls += 1
        return self.finish_up_answer

    async def first_run_done(self) -> None:
        self.first_run_calls += 1
        if self.data:
            self.data["first_run_done"] = True

    async def request_shutdown(self) -> ShutdownOutcome | None:
        self.shutdown_calls += 1
        return self.shutdown_answer

    async def switch_user(self) -> bool:
        self.switch_calls += 1
        return self.switch_answer

    async def report_visit(self, host: str) -> None:
        self.visits.append(host)

    async def report_refused_search(self, words: str, host: str) -> None:
        self.searches.append((words, host))

    async def english_state(self) -> dict[str, Any] | None:
        return self.english

    async def english_answer(self, item: str, right: bool) -> int | None:
        self.english_answers.append((item, right))
        return 1 if item.count("/") == 1 else None

    async def hear_assistant(self, audio_b64: str, language: str = "") -> str | None:
        self.heard.append(language)
        return self.hear_text

    async def ask_assistant(self, question: str, **chat: Any) -> dict[str, Any]:
        self.chat_asked.append((question, chat))
        return {"refused": False, "done": True, "sentences": ["A dog! Great!"], "text": ""}

    async def ask_a_parent(self, url: str, kind: str = "site") -> tuple[bool, str]:
        self.asked.append(url)
        self.asked_kinds.append(kind)
        return True, ""


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
    return TestClient(app, base_url=LAUNCHER)


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


def _css_rule(css: str, selectors: str) -> str:
    """The declarations of the rule whose selector list is exactly `selectors`."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    wanted = [s.strip() for s in selectors.split(",")]
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        if [s.strip() for s in match.group(1).split(",")] == wanted:
            return match.group(2)
    raise AssertionError(f"no rule for {selectors}")


def _css_px(css: str, name: str) -> int:
    match = re.search(rf"{re.escape(name)}:\s*(\d+)px", css)
    assert match is not None, name
    return int(match.group(1))


def _per_row(screen: int, tile: int, gap: int, side: int) -> int:
    """How many tiles a wrapping row holds between the grid's side paddings."""
    return (screen - 2 * side + gap) // (tile + gap)


def test_a_row_that_does_not_fill_is_centred(client: TestClient) -> None:
    """Sixteen tiles six a row ended in four and two holes on the right, and
    the owner said it looked empty (D-210). Every row is centred now, the last
    one too, on the home screen and on the browser's start page alike; and the
    grid still scrolls inside itself, with the tiles in the page's order."""
    css = (STATIC / "shell.css").read_text(encoding="utf-8")
    grid = _css_rule(css, ".tiles, .start-grid")
    for declaration in (
        "display: flex",
        "flex-wrap: wrap",
        "justify-content: center",
        "align-content: safe center",
        "overflow-y: auto",
        "min-height: 0",
    ):
        assert declaration in grid, declaration
    assert "grid-template-columns" not in grid, "a grid's columns hold the last row left"
    assert not re.search(r"(^|[\s;])order:", grid), "Tab follows the page's order"
    assert "flex: none" in _css_rule(css, ".tiles > .tile, .start-grid > .tile"), (
        "a tile keeps its size; the row wraps"
    )
    assert '<main class="tiles">' in client.get("/").text
    assert '<main class="start-grid">' in client.get("/start").text


def test_the_rows_hold_six_at_home_and_seven_on_the_start_page() -> None:
    """On the owner's 1600-pixel screen, as before D-210: six 220-pixel tiles a
    row at home, and seven on the start page, where seven sites put the young
    band's 220-pixel tiles in columns of 203, touching - so there they are 200,
    with a gap."""
    css = (STATIC / "shell.css").read_text(encoding="utf-8")
    tokens = (pathlib.Path(shell_app.SHARED_STATIC_DIR) / "tokens.css").read_text(
        encoding="utf-8"
    )
    side = _css_px(tokens, "--sp-5")
    young = _css_rule(css, '.screen[data-band="4-7"]')
    assert _css_px(young, "--tile-size") == 220
    assert _per_row(1600, 220, _css_px(tokens, "--sp-4"), side) == 6

    start = _css_px(_css_rule(css, '.screen[data-band="4-7"] .start-grid'), "--tile-size")
    assert "gap: var(--sp-3)" in _css_rule(css, ".start-grid")
    assert _per_row(1600, start, _css_px(tokens, "--sp-3"), side) == 7
    assert start >= 3 * _css_px(tokens, "--target-young"), "still a big target for 4-7"


def test_the_young_band_gets_no_clock_but_the_minutes_in_the_ring(
    client: TestClient, daemon: FakeShellDaemon
) -> None:
    """4–7 has no clock (UX.md §2.2), but the ring carries the minutes (D-162).

    The owner asked for the number inside the circle for every child: the one
    who cannot read still has the shrinking circle, and the parent standing
    behind them can see at a glance what is left.
    """
    page = client.get("/").text
    assert 'id="clock"' not in page
    assert 'id="ring-number">42</span>' in page
    assert '<span class="ring__unit" aria-hidden="true">dk</span>' in page


def test_the_older_band_gets_both(client: TestClient, daemon: FakeShellDaemon) -> None:
    assert daemon.data is not None
    daemon.data["age_band"] = AgeBand.OLDER.value
    page = client.get("/").text
    assert 'id="clock"' in page
    assert 'id="ring-number">42</span>' in page


def test_a_screen_reader_hears_the_minutes_left(client: TestClient) -> None:
    page = client.get("/").text
    assert 'aria-label="Kalan süre: 42 dk"' in page


def test_the_ring_says_minutes_in_the_childs_language(
    client: TestClient, daemon: FakeShellDaemon
) -> None:
    assert daemon.data is not None
    daemon.data["locale"] = "en"
    page = client.get("/").text
    assert '<span class="ring__unit" aria-hidden="true">min</span>' in page
    assert 'aria-label="Time left: 42 min"' in page


def test_the_time_warning_shows_the_same_minutes(client: TestClient) -> None:
    """The full-screen pause has a ring too, and a number in it (D-160, D-162)."""
    page = client.get("/").text
    assert 'id="notice-ring-number"' in page
    assert '<span class="notice__ring-unit">dk</span>' in page


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
    config: Config, daemon: FakeShellDaemon
) -> None:
    with _launcher_client(config, daemon) as client:
        response = client.post("/launch/tuxpaint")
        _settle(client)  # the report goes after the answer (D-208)
    assert response.json()["ok"] is True
    assert daemon.launched == ["tuxpaint"]
    # The app, never its kind: that is the daemon's to look up (D-228). This
    # launcher did not keep the app as its tile's, so no scope is named.
    assert daemon.activity == [("tuxpaint", "Boya", "")]


class _TrackingLauncher(_RecordingLauncher):
    """A launcher that keeps an app tile's app, as AppLauncher does."""

    async def launch(self, request: LaunchRequest) -> Any:
        from biokids_shell.launcher import LaunchResult

        self.requests.append(request)
        tracked = request.kind == "app"
        return LaunchResult(ok=True, unit=f"biokids-app-{request.slug}-1", tracked=tracked)


def test_an_app_tile_is_reported_with_the_scope_it_runs_in(
    config: Config, daemon: FakeShellDaemon
) -> None:
    """The scope is what the daemon stops when the app's kind of time is up."""
    app = create_app(config)
    app.state.shell.client = daemon  # type: ignore[assignment]
    app.state.shell.launcher = _TrackingLauncher(config)
    with TestClient(app, base_url=LAUNCHER) as client:
        client.post("/launch/tuxpaint")
        client.post("/launch/eba")
        _settle(client)
    assert daemon.activity == [
        ("tuxpaint", "Boya", "biokids-app-tuxpaint-1.scope"),
        # A web tile's own scope ends as it hands over to the browser already
        # open; the focus reporter finds the browser's.
        ("eba", "EBA", ""),
    ]


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


# ------------------------------------------------- opening at once (D-208)


def _settle(client: TestClient) -> None:
    """Let the work an answer did not wait for finish, on the client's own loop."""

    async def finished() -> None:
        tasks = list(client.app.state.shell.background)  # type: ignore[attr-defined]
        await asyncio.gather(*tasks, return_exceptions=True)

    client.portal.call(finished)  # type: ignore[union-attr]


def _launcher_client(config: Config, daemon: FakeShellDaemon) -> TestClient:
    app = create_app(config)
    app.state.shell.client = daemon  # type: ignore[assignment]
    app.state.shell.launcher = _RecordingLauncher(config)
    return TestClient(app, base_url=LAUNCHER)


class _SlowToNoteDaemon(FakeShellDaemon):
    """A daemon that takes its time over the activity report: until `release`."""

    def __init__(self, state: dict[str, Any]) -> None:
        super().__init__(state)
        self.release: asyncio.Event | None = None

    async def report_activity(self, app_slug: str, app_name: str = "", unit: str = "") -> None:
        assert self.release is not None
        await self.release.wait()
        await super().report_activity(app_slug, app_name, unit)


def test_the_answer_does_not_wait_for_the_activity_report(config: Config) -> None:
    """The child's tap is answered; the daemon hears what was opened just after."""
    daemon = _SlowToNoteDaemon(a_state())
    with _launcher_client(config, daemon) as client:

        async def event() -> asyncio.Event:
            return asyncio.Event()  # made on the client's own loop

        daemon.release = client.portal.call(event)  # type: ignore[union-attr]
        assert client.post("/launch/tuxpaint").json() == {"ok": True, "reason": ""}
        assert daemon.activity == [], "the answer waited for the report"
        assert len(client.app.state.shell.background) == 1  # type: ignore[attr-defined]

        client.portal.call(daemon.release.set)  # type: ignore[union-attr]
        _settle(client)
        assert daemon.activity == [("tuxpaint", "Boya", "")]
        assert not client.app.state.shell.background  # type: ignore[attr-defined]


def test_a_report_that_fails_is_logged_not_raised(
    config: Config, caplog: pytest.LogCaptureFixture
) -> None:
    class Broken(FakeShellDaemon):
        async def report_activity(
            self, app_slug: str, app_name: str = "", unit: str = ""
        ) -> None:
            raise RuntimeError("the daemon hiccuped")

    with _launcher_client(config, Broken(a_state())) as client:
        assert client.post("/launch/tuxpaint").json()["ok"] is True
        _settle(client)
    assert "report-activity failed" in caplog.text


def test_the_tile_and_the_permission_are_asked_together(config: Config) -> None:
    """Two questions to the daemon, not one after the other."""

    class Overlapping(FakeShellDaemon):
        def __init__(self) -> None:
            super().__init__(a_state())
            self.in_state = False
            self.overlapped = False

        async def state(self) -> dict[str, Any] | None:
            self.in_state = True
            await asyncio.sleep(0.05)
            self.in_state = False
            return self.data

        async def may_launch(self, app_slug: str) -> tuple[bool, str]:
            self.overlapped = self.in_state
            return await super().may_launch(app_slug)

    daemon = Overlapping()
    client = _launcher_client(config, daemon)
    assert client.post("/launch/tuxpaint").json()["ok"] is True
    assert daemon.overlapped
    assert daemon.launched == ["tuxpaint"]


def test_the_daemons_no_still_wins_when_asked_together(
    client: TestClient, daemon: FakeShellDaemon
) -> None:
    """Asked at once, the permission is still the daemon's: a refusal starts nothing."""
    daemon.launch_answer = (False, Reason.CATEGORY_EXHAUSTED)
    body = client.post("/launch/tuxpaint").json()
    assert body == {"ok": False, "reason": Reason.CATEGORY_EXHAUSTED}
    assert client.app.state.shell.launcher.requests == []  # type: ignore[attr-defined]
    assert daemon.activity == []


FORM = {"content-type": "application/x-www-form-urlencoded"}


def test_without_javascript_the_launching_screen_has_a_button(client: TestClient) -> None:
    page = client.get("/launch/tuxpaint").text
    noscript = page[page.index("<noscript>") : page.index("</noscript>")]
    assert '<form method="post" action="/launch/tuxpaint">' in noscript
    assert ">Aç</button>" in noscript


def test_without_javascript_nothing_says_it_is_opening(client: TestClient) -> None:
    """Nothing opens until the child presses "Aç", and a four-year-old cannot
    read "Boya açılıyor" above it: the icon, the name and the button. The
    sentence is there for the script, which shows it as it posts."""
    page = client.get("/launch/tuxpaint").text
    body = page[page.index("<body") :]
    status = body[body.index('<p id="launch-status"') :]
    assert status.index(">") < status.index("Boya açılıyor")
    assert "hidden" in status[: status.index(">")]
    assert body.count("açılıyor") == 1, (
        "the sentence is somewhere a child without a script sees"
    )
    script = (STATIC / "shell.js").read_text(encoding="utf-8")
    launching = script[script.index('getElementById("launch-slug")') :]
    assert 'getElementById("launch-status")' in launching
    assert "launchStatus.hidden = false" in launching


def test_a_plain_form_is_answered_with_where_to_go_next(
    client: TestClient, daemon: FakeShellDaemon
) -> None:
    opened = client.post("/launch/tuxpaint", content=b"", headers=FORM, follow_redirects=False)
    assert opened.status_code == 303 and opened.headers["location"] == "/"
    assert daemon.launched == ["tuxpaint"]

    daemon.launch_answer = (False, Reason.QUOTA_EXHAUSTED)
    refused = client.post("/launch/tuxpaint", content=b"", headers=FORM, follow_redirects=False)
    assert refused.status_code == 303
    assert refused.headers["location"] == (
        f"/blocked?reason={Reason.QUOTA_EXHAUSTED}&slug=tuxpaint"
    )

    stranger = client.post("/launch/doom", content=b"", headers=FORM, follow_redirects=False)
    assert stranger.headers["location"].startswith("/blocked?reason=")


def test_a_form_from_a_website_still_cannot_start_an_app(
    client: TestClient, daemon: FakeShellDaemon
) -> None:
    headers = {**FORM, "origin": "https://evil.example", "sec-fetch-site": "cross-site"}
    response = client.post("/launch/tuxpaint", content=b"", headers=headers)
    assert response.status_code == 403
    assert daemon.launched == []


def test_a_tap_opens_the_app_from_the_home_page(client: TestClient) -> None:
    """No screen in between: the tile shows it is opening, and stays a link."""
    page = client.get("/").text
    assert 'href="/launch/tuxpaint"' in page, "without a script the link still works"
    strings = json.loads(
        page.split('<script id="strings" type="application/json">')[1].split("<")[0]
    )
    assert strings["opening"] == "Açılıyor…"
    script = (STATIC / "shell.js").read_text(encoding="utf-8")
    section = script[
        script.index("opening (D-208)") : script.index(
            "/* ---", script.index("opening (D-208)")
        )
    ]
    for needed in (
        'a[href^="/launch/"]',
        "preventDefault",
        'method: "POST"',
        "is-opening",
        "OPENING_MS = 25000",
        '"blur"',
        "visibilitychange",
        "/blocked?reason=",
        "Date.now() - busy.since < RETAP_MS",
    ):
        assert needed in section, needed


def _js_function(script: str, name: str) -> str:
    """The body of `var <name> = function (...) {...};` in shell.js."""
    start = script.index(f"var {name} = function")
    return script[start : script.index("\n  };\n", start)]


def test_a_no_for_a_tap_the_child_has_left_is_not_shown() -> None:
    """The 25 seconds ran out, an app's window took the focus, or a later tap
    has its own ring: a "no" arriving then is about nothing the child can see,
    and the calm screen must not jump over whatever they are doing now."""
    script = (STATIC / "shell.js").read_text(encoding="utf-8")
    refused = _js_function(script, "refused")
    assert "opening[slug] === entry" in refused
    assert "!entry.superseded" in refused
    assert refused.index("if (!live) return;") < refused.index("window.location.href")


def test_an_app_that_ends_as_it_starts_ends_its_tiles_opening() -> None:
    """The yes comes at the exec, before an app that cannot start has failed:
    without a second look, its tile said "Açılıyor…" for 25 seconds over
    nothing. An app tile's yes is followed by a look at /running, after the
    launcher's own watch is over, and a tile whose app is gone gets the calm
    screen - through `refused`, so only while its ring is still the live one."""
    script = (STATIC / "shell.js").read_text(encoding="utf-8")
    open_app = _js_function(script, "openApp")
    assert "result.tracked" in open_app and "checkStillThere(slug, entry)" in open_app
    check = _js_function(script, "checkStillThere")
    assert "if (opening[slug] !== entry) return;" in check
    assert "askRunning()" in check
    assert 'refused(slug, entry, "error.unknown")' in check
    assert 'fetch("/running"' in _js_function(script, "askRunning")
    match = re.search(r"var STILL_THERE_MS = (\d+);", script)
    assert match is not None
    assert int(match.group(1)) > launcher_module.SCOPE_REFUSAL_TIMEOUT_S * 1000, (
        "the page looks before the launcher has finished watching"
    )


def test_an_app_tiles_yes_says_the_page_can_follow_it(
    config: Config, daemon: FakeShellDaemon
) -> None:
    class Following(_RecordingLauncher):
        async def launch(self, request: LaunchRequest) -> Any:
            from biokids_shell.launcher import LaunchResult

            self.requests.append(request)
            return LaunchResult(ok=True, unit=f"biokids-app-{request.slug}-1", tracked=True)

    app = create_app(config)
    app.state.shell.client = daemon  # type: ignore[assignment]
    app.state.shell.launcher = Following(config)
    with TestClient(app, base_url=LAUNCHER) as client:
        assert client.post("/launch/tuxpaint").json() == {
            "ok": True,
            "reason": "",
            "tracked": True,
        }
        form = client.post(
            "/launch/tuxpaint", content=b"", headers=FORM, follow_redirects=False
        )
        assert form.status_code == 303 and form.headers["location"] == "/"
        daemon.launch_answer = (False, Reason.QUOTA_EXHAUSTED)
        assert "tracked" not in client.post("/launch/tuxpaint").json()


def test_the_opening_tile_is_seen_and_is_still(client: TestClient) -> None:
    css = (STATIC / "shell.css").read_text(encoding="utf-8")
    section = css[css.index("opening (D-208)") :]
    assert ".tile.is-opening::before" in section
    assert ".tile__opening" in section
    assert "font-size: var(--tile-label)" in section, "as big as the tile's own name"
    still = section[section.index("prefers-reduced-motion") :]
    assert ".tile.is-opening::before" in still and "animation: none" in still


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


@pytest.mark.parametrize(
    ("reason", "bedtime", "cards", "title"),
    [
        (Reason.QUOTA_EXHAUSTED, False, 1, "Şimdi ne yapsak?"),
        (Reason.QUOTA_EXHAUSTED, True, 1, "Şimdi ne yapsak?"),
        (Reason.WINDOW_WAITING, False, 1, "Şimdi ne yapsak?"),
        (Reason.BREAK_ACTIVE, False, 3, "Haydi biraz hareket!"),
        (Reason.PENALTY_ACTIVE, False, 0, None),
    ],
)
def test_the_screens_that_end_the_time_say_what_to_do_next(
    client: TestClient,
    daemon: FakeShellDaemon,
    reason: str,
    bedtime: bool,
    cards: int,
    title: str | None,
) -> None:
    """D-231: away from the screen, and Zio says it."""
    assert daemon.data is not None
    daemon.data.update(
        allow=False, reason_code=reason, bedtime=bedtime, break_seconds=300, penalty_seconds=300
    )
    page = client.get("/").text
    assert page.count('class="offscreen__card"') == cards
    if title:
        assert title in page
        assert "data-offscreen-say=" in page
    else:
        assert "offscreen" not in page.split("<body", 1)[1].split("<script", 1)[0]


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
    assert "Bilgisayar kapanıyor" in done.text
    assert "Yarın görüşürüz" in done.text


def test_the_goodbye_is_said_aloud_for_a_child_who_cannot_read_yet(
    client: TestClient, daemon: FakeShellDaemon
) -> None:
    """D-175's rule for full screens; the daemon waits a few seconds for it (D-209)."""
    assert "data-speak-on-load" in client.post("/shutdown").text
    assert daemon.data is not None
    daemon.data["age_band"] = AgeBand.OLDER.value
    assert "data-speak-on-load" not in client.post("/shutdown").text


def test_the_goodbye_is_never_a_dead_end(client: TestClient) -> None:
    """Should the machine stay up after all (the daemon restarted mid-way), the
    child goes back to the launcher's current screen after a minute (D-209)."""
    assert 'id="goodbye"' in client.post("/shutdown").text
    script = (
        pathlib.Path(__file__).resolve().parent.parent / "src/biokids_shell/static/shell.js"
    ).read_text(encoding="utf-8")
    assert 'getElementById("goodbye")' in script


def test_with_someone_else_logged_in_the_child_is_told_what_happens_instead(
    client: TestClient, daemon: FakeShellDaemon
) -> None:
    """The computer stays on and only the child's session ends (D-100, D-209).

    This used to re-render the home screen and say nothing, so the child pressed
    "Kapat" again, and again.
    """
    daemon.shutdown_answer = ShutdownOutcome.LOGOUT
    page = client.post("/shutdown").text
    text = _visible_text(page)
    assert "başka biri de var" in text
    assert "açık kalıyor" in text
    assert "oturumunu kapatıyorum" in text
    assert "Bilgisayar kapanıyor" not in text
    assert "Merhaba Elif" not in text, "never the home screen"
    assert "data-speak-on-load" in page


def test_the_same_in_english(client: TestClient, daemon: FakeShellDaemon) -> None:
    assert daemon.data is not None
    daemon.data["locale"] = "en"
    assert "The computer is shutting down" in client.post("/shutdown").text
    daemon.shutdown_answer = ShutdownOutcome.LOGOUT
    assert "so it stays on" in client.post("/shutdown").text


def test_a_shutdown_the_daemon_never_heard_is_said_so_calmly(
    client: TestClient, daemon: FakeShellDaemon
) -> None:
    """Never the home screen, as if nothing had been pressed (D-209).

    An IPC failure used to put the child back on the launcher: they could not
    tell "it did not work" from "it is about to", and walked away from an open
    session. They are told, without an error code, to find a grown-up, and
    have a way back.
    """
    daemon.shutdown_answer = None
    page = client.post("/shutdown").text
    text = _visible_text(page)
    assert "Bilgisayar kapanamadı" in text
    assert "Bir büyüğüne söyle" in text
    assert "Merhaba Elif" not in text
    assert 'href="/"' in page
    assert 'action="/shutdown"' not in page, "no second try that would fail the same way"


def test_the_failure_page_needs_no_daemon_to_draw(config: Config) -> None:
    """The likeliest reason the request failed is that the daemon is gone."""
    app = create_app(config)
    gone = FakeShellDaemon(None)
    gone.shutdown_answer = None
    app.state.shell.client = gone  # type: ignore[assignment]
    page = TestClient(app, base_url=LAUNCHER).post("/shutdown").text
    assert "Bilgisayar kapanamadı" in page


class _AnsweringClient:
    """Stands in for the socket under ShellClient: one fixed answer, or a failure."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer

    async def call(self, op: Any, **_args: Any) -> Any:
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ({"ok": True, "data": {"poweroff": True, "outcome": "poweroff"}}, "poweroff"),
        ({"ok": True, "data": {"poweroff": False, "outcome": "logout"}}, "logout"),
        ({"ok": True, "data": {"poweroff": True}}, "poweroff"),
        ({"ok": True, "data": {"outcome": "explode"}}, None),
        ({"ok": False, "error": "shell.not_allowed"}, None),
    ],
)
async def test_the_client_reads_what_the_daemon_will_do(
    tmp_path: pathlib.Path, answer: dict[str, Any], expected: str | None
) -> None:
    from biokids_core.ipc import Response
    from biokids_shell.client import ShellClient

    client = ShellClient(tmp_path / "shell.sock")
    client._client = _AnsweringClient(Response(**answer))  # type: ignore[assignment]
    outcome = await client.request_shutdown()
    assert (outcome.value if outcome else None) == expected


@pytest.mark.parametrize(
    "failure", [IpcError("gone"), TimeoutError(), ConnectionRefusedError()]
)
async def test_the_client_says_none_when_the_daemon_cannot_be_asked(
    tmp_path: pathlib.Path, failure: Exception
) -> None:
    """A timeout included: that is what five seconds of stopping apps looked like."""
    from biokids_shell.client import ShellClient

    client = ShellClient(tmp_path / "shell.sock")
    client._client = _AnsweringClient(failure)  # type: ignore[assignment]
    assert await client.request_shutdown() is None


def test_the_launcher_holds_when_the_daemon_is_gone(config: Config) -> None:
    """A working launcher with no limits behind it is the failure to avoid."""
    app = create_app(config)
    app.state.shell.client = FakeShellDaemon(None)  # type: ignore[assignment]
    client = TestClient(app, base_url=LAUNCHER)

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
    page = TestClient(app, base_url=LAUNCHER).get("/").text

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
        wording[action] = TestClient(app, base_url=LAUNCHER).get("/").text

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
    page = TestClient(app, base_url=LAUNCHER).get("/").text

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
    assert policy["URLBlocklist"][0] == "*"
    assert policy["URLAllowlist"] == ["127.0.0.1:7879"]
    assert policy["URLBlocklistExceptions"] == []


def test_the_policy_lets_the_childs_web_tiles_through() -> None:
    """The file is machine-wide, so it governs the tile's window as well (D-156).

    With only the launcher allowed, every web tile opened on "This page is
    blocked". Hosts arrive without `www.` so their subdomains are covered too.
    """
    policy = kiosk_policy("127.0.0.1:7879", ["trtcocuk.net.tr", "eba.gov.tr", "eba.gov.tr"])

    assert policy["URLAllowlist"] == ["127.0.0.1:7879", "eba.gov.tr", "trtcocuk.net.tr"]
    assert policy["URLBlocklist"][0] == "*", "everything else stays closed"


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


def test_no_invitation_to_sign_in() -> None:
    """A new profile offered "Chromium'da oturum açmak istiyor musunuz?" on the
    VM although sign-in itself was off."""
    policy = kiosk_policy("127.0.0.1:7879")
    assert policy["BrowserSignin"] == 0
    assert policy["SigninInterceptionEnabled"] is False
    assert policy["PromotionsEnabled"] is False


def test_the_address_bar_searches_google_and_suggests_nothing() -> None:
    """D-202: an address-bar search goes to SafeSearch Google, where the
    extension's rules see it; suggestions, which the browser fetches itself
    out of any extension's sight, are off, and nothing is loaded ahead."""
    policy = kiosk_policy("127.0.0.1:7879", mode="blocklist")
    assert policy["DefaultSearchProviderEnabled"] is True
    assert str(policy["DefaultSearchProviderSearchURL"]).startswith(
        "https://www.google.com/search?q={searchTerms}"
    )
    assert policy["ForceGoogleSafeSearch"] is True
    assert policy["SearchSuggestEnabled"] is False
    assert policy["NetworkPredictionOptions"] == 2


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


def test_the_kiosk_window_can_be_found_to_bring_a_warning_forward(config: Config) -> None:
    """D-160: the relay finds the launcher among the app windows by its class."""
    from biokids_shell.kiosk import KIOSK_WINDOW_CLASS

    assert f"--class={KIOSK_WINDOW_CLASS}" in KioskSupervisor(config).argv()


def test_every_screen_carries_the_full_screen_warning(client: TestClient) -> None:
    """D-160: not a strip an app can cover - the character, the sentence, the ring."""
    page = client.get("/").text
    assert 'id="notice"' in page
    assert 'data-face="zio"' in page and 'data-face="sio"' in page
    assert "window.BIOKIDS_NOTICE_MS = 7000" in page


def test_the_page_can_ask_what_is_actually_being_said(client: TestClient) -> None:
    """The card shows the sentence the voice service chose, not a different one."""
    assert client.get("/spoken").json() == {}

    client.app.state.shell.remember_spoken(  # type: ignore[attr-defined]
        {"event": "warn_10", "text": "On dakikan kaldı.", "persona": "sio", "at": 1.0}
    )
    spoken = client.get("/spoken").json()
    assert spoken["text"] == "On dakikan kaldı."
    assert spoken["persona"] == "sio"


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


def test_a_game_in_usr_games_starts_by_its_full_path(
    config: Config, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user service's PATH has no /usr/games. GCompris, Frozen Bubble and
    TuxMath were drawn but did not start: the launcher looked for them by name."""
    import os

    import biokids_core.catalog

    games = tmp_path / "games"
    games.mkdir()
    # Windows finds a program only by an extension in PATHEXT.
    program = games / ("gcompris-qt.exe" if os.name == "nt" else "gcompris-qt")
    program.write_bytes(b"#!/bin/sh\n")
    program.chmod(0o755)
    monkeypatch.setattr(biokids_core.catalog, "GAMES_DIRS", (str(games),))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    argv = AppLauncher(config).build_argv(
        LaunchRequest(
            slug="gcompris", name="GCompris", kind="app", exec_cmd="gcompris-qt --window"
        )
    )
    assert argv is not None
    assert pathlib.Path(argv[0]).resolve() == program.resolve()
    assert argv[1:] == ["--window"]


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

    monkeypatch.setattr(shutil, "which", lambda _n, **_kw: "/usr/bin/systemd-run")
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

    monkeypatch.setattr(shutil, "which", lambda _n, **_kw: "/usr/bin/systemd-run")
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


# ------------------------------------------------- after an upgrade (D-165)


def test_static_files_are_checked_before_every_use(client: TestClient) -> None:
    """A packaged file's old timestamp made the browser keep yesterday's launcher."""
    response = client.get("/static/shell.css")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"


def test_stylesheets_and_scripts_carry_a_version(client: TestClient) -> None:
    from biokids_shell.app import asset_fingerprint

    page = client.get("/").text
    version = asset_fingerprint()
    assert len(version) == 12
    assert f"/static/shell.css?v={version}" in page
    assert f"/static/shell.js?v={version}" in page


def test_an_app_that_is_not_installed_is_not_drawn(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-164: on a machine without GCompris its tile was drawn and did nothing."""
    import biokids_shell.app

    monkeypatch.setattr(biokids_shell.app, "program_installed", lambda _program: False)
    page = client.get("/").text
    assert 'data-slug="tuxpaint"' not in page
    assert 'data-slug="eba"' in page or "EBA" in page or "tile" in page


def test_games_are_found_where_debian_puts_them(monkeypatch: pytest.MonkeyPatch) -> None:
    """A systemd service's PATH has no /usr/games; the panel thought every game missing."""
    from biokids_core import catalog

    monkeypatch.undo()  # the suite pretends every program is installed
    seen: list[str] = []
    monkeypatch.setattr(
        catalog.shutil, "which", lambda _p, path=None: seen.append(path or "") or None
    )
    catalog.program_installed("tuxmath")
    assert "/usr/games" in seen[0]


# ------------------------------------------------ the open internet (D-167)


def test_open_mode_has_no_wildcard_and_keeps_chromiums_own_pages_closed() -> None:
    """Harmful sites are the DNS filter's job; the browser keeps its own pages shut."""
    policy = kiosk_policy("127.0.0.1:7879", ["eba.gov.tr"], mode="blocklist")
    blocked = policy["URLBlocklist"]
    assert isinstance(blocked, list)
    assert "*" not in blocked
    assert "chrome://settings" in blocked and "chrome://flags" in blocked
    assert "file://*" in blocked, "Ctrl+O browses the disk"
    assert policy["SSLErrorOverrideAllowed"] is False


def test_the_policy_does_not_close_the_parents_panel() -> None:
    """Chromium's policy is machine-wide. The panel is closed to the children by
    nftables (render_nft), so the parent's own browser opens it from the menu."""
    blocked = kiosk_policy("127.0.0.1:7879", mode="allowlist")["URLBlocklist"]
    assert isinstance(blocked, list)
    assert not any("7878" in str(entry) for entry in blocked)


def test_safe_search_is_forced_in_every_mode() -> None:
    for mode in ("allowlist", "blocklist"):
        policy = kiosk_policy("127.0.0.1:7879", mode=mode)
        assert policy["ForceGoogleSafeSearch"] is True
        assert policy["ForceYouTubeRestrict"] == 2
        assert policy["DnsOverHttpsMode"] == "off", "or DNS filtering is gone round"


def test_chromes_own_pages_are_closed_even_with_the_wildcard() -> None:
    """Since Chromium 147 a bare * no longer covers chrome:// pages."""
    blocked = kiosk_policy("127.0.0.1:7879")["URLBlocklist"]
    assert isinstance(blocked, list)
    assert "chrome://settings" in blocked


# ------------------------------------------------- only the launcher's own page


def test_a_website_cannot_power_the_machine_off(
    client: TestClient, daemon: FakeShellDaemon
) -> None:
    """Any page the child opens could submit a form to 127.0.0.1:7879."""
    response = client.post(
        "/shutdown",
        headers={"origin": "https://example.com", "sec-fetch-site": "cross-site"},
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "path", ["/shutdown", "/ask-a-parent", "/first-run", "/device/volume?value=100"]
)
def test_every_change_refuses_another_site(client: TestClient, path: str) -> None:
    response = client.post(path, headers={"sec-fetch-site": "cross-site"})
    assert response.status_code == 403


def test_a_rebound_name_is_not_the_launcher(client: TestClient) -> None:
    """evil.example resolving to 127.0.0.1 is same-origin with itself, not with us."""
    response = client.get("/state", headers={"host": "evil.example:7879"})
    assert response.status_code == 421


def test_the_launchers_own_page_still_works(client: TestClient) -> None:
    response = client.post(
        "/device/volume?value=40",
        headers={"origin": LAUNCHER, "sec-fetch-site": "same-origin"},
    )
    assert response.status_code == 200


@pytest.mark.parametrize("path", ["/shutdown", "/ask-a-parent", "/first-run"])
def test_a_form_the_launcher_posts_itself_is_not_refused(
    client: TestClient, daemon: FakeShellDaemon, path: str
) -> None:
    """What Chromium sent for "Büyüğüme sor" on the VM (D-204): a form posted by
    a page under no-referrer carries "Origin: null". The launcher answered 403,
    a blank page, and the child's question went nowhere."""
    chromium = {"origin": "null", "sec-fetch-site": "same-origin"}
    response = client.post(path, data={"kind": "search"}, headers=chromium)
    assert response.status_code == 200


@pytest.mark.parametrize("site", ["cross-site", "same-site", None])
def test_a_null_origin_from_anywhere_else_is_refused(
    client: TestClient, site: str | None
) -> None:
    """A sandboxed frame on a website also says "Origin: null"; only the
    browser's own Sec-Fetch-Site tells the two apart."""
    headers = {"origin": "null"} | ({"sec-fetch-site": site} if site else {})
    response = client.post("/shutdown", headers=headers)
    assert response.status_code == 403


def test_back_from_a_block_in_the_browser_is_the_start_page(client: TestClient) -> None:
    """The owner pressed "Geri dön" under a refused search and the launcher's
    whole home screen opened inside a browser tab (D-204)."""
    page = client.get("/blocked?reason=search.blocked").text
    assert 'href="/start"' in page
    assert 'name="browser" value="1"' in page
    site = client.get("/blocked?url=https://closed.test/").text
    assert 'href="/start"' in site


def test_back_from_a_block_in_the_launcher_is_home(client: TestClient) -> None:
    page = client.get("/blocked?reason=app.not_allowed&slug=tuxpaint").text
    assert 'href="/start"' not in page
    assert 'name="browser"' not in page


def test_back_after_asking_from_the_browser_is_the_start_page(client: TestClient) -> None:
    same = {"origin": LAUNCHER, "sec-fetch-site": "same-origin"}
    asked = client.post("/ask-a-parent", data={"kind": "search", "browser": "1"}, headers=same)
    assert 'href="/start"' in asked.text
    home = client.post("/ask-a-parent", data={"kind": "time"}, headers=same)
    assert 'href="/start"' not in home.text


def test_the_launchers_pages_send_their_origin_with_a_form(client: TestClient) -> None:
    """no-referrer made every form post "Origin: null" (D-204); same-origin keeps
    the launcher's address from other sites just the same."""
    page = client.get("/blocked?reason=search.blocked").text
    assert '<meta name="referrer" content="same-origin">' in page
    assert 'content="no-referrer"' not in page


# -------------------------------------------------- D-173 asking for things


def test_out_of_minutes_asks_for_time(client: TestClient) -> None:
    page = client.get("/blocked?reason=quota.exhausted").text
    assert 'name="kind" value="time"' in page


def test_a_used_up_kind_of_app_offers_no_question_it_cannot_answer(
    client: TestClient,
) -> None:
    """A granted ten minutes goes to the day, not to games: asking would be a
    button that does nothing. The page says what happened instead (D-193)."""
    page = client.get("/blocked?reason=category.exhausted&slug=supertux").text
    assert "/ask-a-parent" not in page
    assert "bugünkü süren doldu" in page


def test_an_app_that_is_not_on_the_screen_asks_for_that_app(client: TestClient) -> None:
    page = client.get("/blocked?reason=app.not_allowed&slug=tuxpaint").text
    assert 'name="kind" value="app"' in page and 'value="tuxpaint"' in page


def test_the_request_carries_its_kind(client: TestClient, daemon: FakeShellDaemon) -> None:
    response = client.post(
        "/ask-a-parent",
        data={"kind": "time"},
        headers={"origin": LAUNCHER, "sec-fetch-site": "same-origin"},
    )
    assert response.status_code == 200
    assert daemon.asked_kinds == ["time"]


# ---------------------------------------------- D-175 for a child who cannot read


class SaidAloud:
    def __init__(self) -> None:
        self.said: list[tuple[str, str]] = []

    async def __call__(self, text: str, locale: str) -> None:
        self.said.append((text, locale))


def test_a_tiles_name_is_read_out_on_request(client: TestClient) -> None:
    said = SaidAloud()
    client.app.state.shell.say = said  # type: ignore[attr-defined]
    response = client.post(
        "/speak-label",
        json={"text": "  Boya  "},
        headers={"origin": LAUNCHER, "sec-fetch-site": "same-origin"},
    )
    assert response.json() == {"ok": True}
    assert said.said == [("Boya", "tr")]


def test_a_website_cannot_make_the_launcher_talk(client: TestClient) -> None:
    said = SaidAloud()
    client.app.state.shell.say = said  # type: ignore[attr-defined]
    response = client.post(
        "/speak-label",
        content="text=hello",
        headers={"content-type": "application/x-www-form-urlencoded", "origin": LAUNCHER},
    )
    assert response.status_code == 403 and said.said == []


def test_the_young_bands_ring_has_a_setting_sun(client: TestClient) -> None:
    page = client.get("/").text
    assert 'id="ring-sun"' in page
    assert 'id="ring-number"' in page, "the minutes stay, for every age (D-162)"


def test_the_older_band_keeps_the_plain_ring(config: Config, daemon: FakeShellDaemon) -> None:
    daemon.data = a_state(age_band="8-10")
    app = create_app(config)
    app.state.shell.client = daemon  # type: ignore[assignment]
    page = TestClient(app, base_url=LAUNCHER).get("/").text
    assert 'id="ring-sun"' not in page


def test_a_screen_with_a_sentence_says_it_for_the_young_band(client: TestClient) -> None:
    assert "data-speak-on-load" in client.get("/blocked").text


# --------------------------------------------- D-187, D-188 extension and ports


def test_each_child_has_a_launcher_port_of_their_own(config: Config) -> None:
    assert config.shell.launcher_port(1001) == 7880
    assert config.shell.launcher_port(1002) == 7881
    assert config.shell.launcher_origin(1001) == "127.0.0.1:7880"


def test_the_policy_names_every_launcher_and_forces_the_extension() -> None:
    ext = ("a" * 32, "file:///var/lib/biokids-ext/updates.xml")
    policy = kiosk_policy(["127.0.0.1:7880", "127.0.0.1:7881"], extension=ext)
    allowed = policy["URLAllowlist"]
    assert isinstance(allowed, list)
    assert "127.0.0.1:7880" in allowed and "127.0.0.1:7881" in allowed
    assert policy["AudioCaptureAllowedUrls"] == [
        "http://127.0.0.1:7880",
        "http://127.0.0.1:7881",
    ]
    assert policy["ExtensionInstallForcelist"] == [f"{'a' * 32};{ext[1]}"]


def test_the_extension_finds_its_launcher_by_healthz(client: TestClient) -> None:
    assert client.get("/healthz").json()["biokids"] == "launcher"


def test_healthz_does_not_wait_for_a_slow_daemon(
    client: TestClient, daemon: FakeShellDaemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The extension gives up on a slow answer, and then a refused search stays
    on Chromium's own error page instead of the launcher's (D-204)."""
    import time

    async def slow() -> bool:
        await asyncio.sleep(30)
        return True

    monkeypatch.setattr(daemon, "available", slow)
    started = time.monotonic()
    body = client.get("/healthz").json()
    assert time.monotonic() - started < 5
    assert body["biokids"] == "launcher"
    assert body["daemon"] is False


def write_extension_id(config: Config, ext_id: str = "b" * 32) -> str:
    directory = config.paths.extension_dir
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "extension-id").write_text(ext_id, encoding="ascii")
    return ext_id


def test_the_extension_reports_a_host(
    client: TestClient, daemon: FakeShellDaemon, config: Config
) -> None:
    ext_id = write_extension_id(config)
    response = client.post(
        "/visit",
        json={"host": "www.eba.gov.tr"},
        headers={"origin": f"chrome-extension://{ext_id}", "sec-fetch-site": "none"},
    )
    assert response.status_code == 204
    assert daemon.visits == ["www.eba.gov.tr"]


def test_only_a_host_name_is_ever_accepted(
    client: TestClient, daemon: FakeShellDaemon, config: Config
) -> None:
    ext_id = write_extension_id(config)
    for host in ("eba.gov.tr/ders?id=5", "http://x.com", "a b"):
        response = client.post(
            "/visit", json={"host": host}, headers={"origin": f"chrome-extension://{ext_id}"}
        )
        assert response.status_code == 400
    assert daemon.visits == []


def test_another_extension_or_a_site_cannot_report_visits(
    client: TestClient, daemon: FakeShellDaemon, config: Config
) -> None:
    write_extension_id(config)
    for origin in (f"chrome-extension://{'c' * 32}", "https://example.com"):
        response = client.post(
            "/visit",
            json={"host": "example.com"},
            headers={"origin": origin, "sec-fetch-site": "cross-site"},
        )
        assert response.status_code == 403
    assert daemon.visits == []


# ------------------------------------------- a refused search, for the parent (D-222)


def test_a_refused_search_reaches_the_daemon_as_its_words(
    client: TestClient, daemon: FakeShellDaemon, config: Config
) -> None:
    ext_id = write_extension_id(config)
    response = client.post(
        "/search-refused",
        json={"url": "https://www.google.com/search?q=porno+videolar%C4%B1&sca_esv=abc"},
        headers={"origin": f"chrome-extension://{ext_id}", "sec-fetch-site": "none"},
    )
    assert response.status_code == 204
    assert daemon.searches == [("porno videoları", "www.google.com")]


def test_an_address_the_list_does_not_refuse_is_not_reported(
    client: TestClient, daemon: FakeShellDaemon, config: Config
) -> None:
    """Whatever is sent, only what the same expressions refuse becomes something
    the parent reads."""
    ext_id = write_extension_id(config)
    for url in (
        "https://www.google.com/search?q=dinozorlar",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "",
    ):
        response = client.post(
            "/search-refused",
            json={"url": url},
            headers={"origin": f"chrome-extension://{ext_id}"},
        )
        assert response.status_code == 204
    assert daemon.searches == []


def test_only_the_extension_reports_a_refused_search(
    client: TestClient, daemon: FakeShellDaemon, config: Config
) -> None:
    """Not another extension, not a website, and not the launcher's own page:
    nothing of the launcher's has a refused search to report."""
    write_extension_id(config)
    url = "https://www.google.com/search?q=porno"
    for headers in (
        {"origin": f"chrome-extension://{'c' * 32}", "sec-fetch-site": "cross-site"},
        {"origin": "https://example.com", "sec-fetch-site": "cross-site"},
        {"origin": "http://testserver", "sec-fetch-site": "same-origin"},
    ):
        response = client.post("/search-refused", json={"url": url}, headers=headers)
        assert response.status_code == 403
    assert daemon.searches == []


def test_the_block_page_tells_the_child_a_grown_up_will_see_it(client: TestClient) -> None:
    """Principle 2: the child knows (D-222)."""
    page = _visible_text(client.get("/blocked?reason=search.blocked").text)
    assert "büyüğün de görecek" in page


# ------------------------------------------------- forbidden searches (D-202)


def test_the_launcher_serves_the_search_rules_with_the_parents_words(
    client: TestClient, daemon: FakeShellDaemon
) -> None:
    from biokids_core import search_filter

    assert daemon.data is not None
    daemon.data["search_words"] = ["kumar"]
    response = client.get("/search-rules")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body == search_filter.payload(["kumar"])
    assert body["complete"] is True
    assert search_filter.address_blocked(
        "https://www.google.com/search?q=kumarhane", body["patterns"]
    )


def test_without_the_daemon_the_launcher_serves_what_it_heard_last(
    client: TestClient, daemon: FakeShellDaemon
) -> None:
    from biokids_core import search_filter

    assert daemon.data is not None
    daemon.data["search_words"] = ["kumar"]
    client.get("/search-rules")
    daemon.data = None

    body = client.get("/search-rules").json()
    assert body["version"] == search_filter.payload(["kumar"])["version"]
    assert body["complete"] is True


def test_the_parents_words_are_not_a_page_in_the_childs_browser(
    client: TestClient, daemon: FakeShellDaemon
) -> None:
    """The launcher's address is open in the child's browser (D-202)."""
    assert daemon.data is not None
    daemon.data["search_words"] = ["kumar"]
    typed = client.get("/search-rules", headers={"sec-fetch-mode": "navigate"})
    assert typed.status_code == 404
    assert "search_words" not in client.get("/state").json()
    fetched = client.get("/search-rules", headers={"sec-fetch-mode": "cors"})
    assert fetched.status_code == 200


def test_never_having_heard_the_words_the_list_is_marked_incomplete(
    client: TestClient, daemon: FakeShellDaemon
) -> None:
    """The extension then keeps a fuller set it already has."""
    daemon.data = None
    body = client.get("/search-rules").json()
    assert body["complete"] is False
    assert body["patterns"], "the default list, never nothing"


def test_a_refused_search_gets_its_own_page_without_the_word(client: TestClient) -> None:
    page = client.get("/blocked?reason=search.blocked&url=https://x.test/?q=porno").text
    text = _visible_text(page)

    assert "Bunu arayamayız" in text
    assert "ona sorabilirsin" in text
    assert "porno" not in page, "the word is never repeated, not even in the form"
    assert 'name="kind" value="search"' in page
    assert 'name="url" value=""' in page
    assert "/ask-a-parent" in page


def test_asking_about_a_refused_search_files_a_search_request(
    client: TestClient, daemon: FakeShellDaemon
) -> None:
    response = client.post("/ask-a-parent", data={"url": "", "kind": "search"})
    assert response.status_code == 200
    assert daemon.asked_kinds == ["search"]
    assert daemon.asked == [""]


@pytest.mark.parametrize(
    ("tool", "output", "expected"),
    [
        ("wpctl", "Volume: 0.40\n", 40),
        ("wpctl", "Volume: 0.35 [MUTED]\n", 35),
        ("pactl", "Volume: front-left: 26214 /  40% / -23.88 dB", 40),
        ("amixer", "  Front Left: Playback 26 [41%] [-23.25dB] [on]", 41),
    ],
)
def test_the_volume_slider_starts_where_the_sound_is(
    monkeypatch: pytest.MonkeyPatch, tool: str, output: str, expected: int
) -> None:
    from biokids_shell import device

    async def fake_output(argv: Any) -> str | None:
        return output if argv[0] == tool else None

    monkeypatch.setattr(device, "_output", fake_output)
    assert asyncio.run(device.get_volume()) == expected


def test_without_a_volume_tool_the_slider_keeps_its_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from biokids_shell import device

    async def nothing(argv: Any) -> str | None:
        return None

    monkeypatch.setattr(device, "_output", nothing)
    assert asyncio.run(device.get_volume()) is None


def test_the_parents_panel_opens_in_the_parents_chromium() -> None:
    """One policy for the whole machine: "selected sites only" closed the panel
    the menu and the desktop shortcut open (D-195). Children are refused the
    port by nftables, not by this list."""
    policy = kiosk_policy(
        ["127.0.0.1:7880"],
        ["eba.gov.tr"],
        mode="allowlist",
        panel=["127.0.0.1:7878", "localhost:7878"],
    )
    allowed = policy["URLAllowlist"]
    assert isinstance(allowed, list)
    assert "127.0.0.1:7878" in allowed and "localhost:7878" in allowed
    assert policy["URLBlocklist"][0] == "*"  # type: ignore[index]


def test_the_kiosk_follows_the_screen_when_it_changes_size() -> None:
    """In the VM the screen grew and the launcher stayed an 800x600 box."""
    from biokids_shell import kiosk

    ran: list[tuple[str, ...]] = []
    stop = asyncio.Event()

    async def fake(*args: str) -> str | None:
        ran.append(args)
        if args[0] == "getdisplaygeometry":
            return "1920 1080\n"
        if args[0] == "search":
            return "4194306\n"
        if args[0] == "getwindowgeometry":
            stop.set()
            return "Window 4194306\n  Position: 560,234 (screen: 0)\n  Geometry: 800x600\n"
        return ""

    asyncio.run(kiosk.keep_fullscreen(stop, fake))

    assert ("windowsize", "4194306", "1920", "1080") in ran
    assert ("windowstate", "--add", "FULLSCREEN", "4194306") in ran


def test_a_kiosk_that_already_fits_is_left_alone() -> None:
    from biokids_shell import kiosk

    ran: list[tuple[str, ...]] = []
    stop = asyncio.Event()

    async def fake(*args: str) -> str | None:
        ran.append(args)
        if args[0] == "getdisplaygeometry":
            return "1920 1080\n"
        if args[0] == "search":
            return "7\n"
        stop.set()
        return "Window 7\n  Position: 0,0 (screen: 0)\n  Geometry: 1920x1080\n"

    asyncio.run(kiosk.keep_fullscreen(stop, fake))
    assert not any(call[0] == "windowsize" for call in ran)


def test_the_sound_is_two_buttons_at_the_top_right(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-199: not a slider in the footer - a four-year-old taps, and looks up."""
    from biokids_shell import device

    async def level() -> int:
        return 40

    monkeypatch.setattr(device, "get_volume", level)
    page = client.get("/").text
    header = page[page.index('<header class="head">') : page.index("</header>")]
    assert 'id="vol" data-level="40"' in header
    assert 'data-step="-10"' in header and 'data-step="10"' in header
    assert 'id="volume"' not in page, "the old footer slider is gone"


STATIC = pathlib.Path(shell_app.STATIC_DIR)


def test_the_sound_bar_can_be_dragged_and_read_by_a_screen_reader(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-201: the bar between the buttons follows a finger, and is a slider to a
    screen reader and to the arrow keys."""
    from biokids_shell import device

    async def level() -> int:
        return 40

    monkeypatch.setattr(device, "get_volume", level)
    page = client.get("/").text
    header = page[page.index('<header class="head">') : page.index("</header>")]
    bar = header[header.index('id="vol-bar"') :]
    assert 'role="slider"' in bar and 'tabindex="0"' in bar
    assert 'aria-valuenow="40"' in bar
    assert 'aria-valuemin="0"' in bar and 'aria-valuemax="100"' in bar
    assert 'id="vol-knob" style="left: 40%"' in bar
    script = (STATIC / "shell.js").read_text(encoding="utf-8")
    for needed in ("pointerdown", "pointermove", "setPointerCapture", "ArrowLeft"):
        assert needed in script
    css = (STATIC / "shell.css").read_text(encoding="utf-8")
    rule = css[css.index(".vol__bar {") :]
    rule = rule[: rule.index("}")]
    assert "touch-action: none" in rule
    assert "height: var(--target-young)" in rule, "the whole height is the target"


def test_the_apps_open_in_the_background_have_a_place_at_the_bottom(
    client: TestClient,
) -> None:
    """D-201: "Açık", filled in by the page from /running; empty, it is hidden."""
    page = client.get("/").text
    foot = page[page.index('<footer class="foot">') : page.index("</footer>")]
    assert 'id="open-apps"' in foot and 'id="open-apps-list"' in foot
    assert "Açık" in foot
    opening = foot[foot.index('id="open-apps"') :]
    assert "hidden" in opening[: opening.index(">")]
    script = (STATIC / "shell.js").read_text(encoding="utf-8")
    assert 'fetch("/running"' in script
    assert '"/launch/" + encodeURIComponent(slug)' in script, "a tap brings it back"
    assert "visibilitychange" in script


def test_the_launcher_says_which_apps_are_open(client: TestClient) -> None:
    async def open_apps() -> list[str]:
        return ["tuxpaint"]

    client.app.state.shell.launcher.open_apps = open_apps  # type: ignore[attr-defined]
    response = client.get("/running")
    assert response.status_code == 200
    assert response.json() == {"running": ["tuxpaint"]}
    assert response.headers["cache-control"] == "no-store"


def test_a_rebound_name_cannot_ask_which_apps_are_open(client: TestClient) -> None:
    assert client.get("/running", headers={"host": "evil.example:7879"}).status_code == 421


# ------------------------------------------------------ the English corner (D-225)

ENGLISH_TILE = {
    "slug": "english",
    "name": "İngilizce",
    "icon": "abc",
    "kind": "builtin",
    "url": "",
    "exec": "",
    "category": "learning",
}


@pytest.fixture
def with_english(daemon: FakeShellDaemon) -> FakeShellDaemon:
    assert daemon.data is not None
    daemon.data["tiles"] = [*daemon.data["tiles"], ENGLISH_TILE]
    return daemon


def _json_post(client: TestClient, path: str, body: dict[str, Any]) -> Any:
    return client.post(
        path,
        json=body,
        headers={"origin": LAUNCHER, "sec-fetch-site": "same-origin"},
    )


def test_the_english_tile_is_a_page_of_the_launcher(
    client: TestClient, with_english: FakeShellDaemon
) -> None:
    answer = client.post("/launch/english", headers={"origin": LAUNCHER}).json()
    assert answer == {"ok": True, "builtin": "english", "page": "/english"}
    assert with_english.launched == [], "no permission asked: it starts no process"


def test_the_corner_opens_on_todays_lesson_and_the_word_of_the_day(
    client: TestClient, with_english: FakeShellDaemon
) -> None:
    page = client.get("/english").text
    assert 'href="/english/lesson"' in page
    assert 'data-say-en="cat"' in page, "before any lesson, the first word"
    assert "0 kelimeyle tanıştın" in _visible_text(page)
    assert page.count("unit-path__unit--now") == 1


def test_without_the_tile_there_is_no_corner(client: TestClient) -> None:
    response = client.get("/english", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/"
    assert client.get("/english/lesson", follow_redirects=False).status_code == 303


def test_the_lesson_page_carries_the_steps_planned_for_today(
    client: TestClient, with_english: FakeShellDaemon
) -> None:
    import json
    import re

    page = client.get("/english/lesson").text
    raw = re.search(
        r'<script type="application/json" id="lesson-data">(.*?)</script>', page, re.S
    )
    assert raw is not None
    lesson = json.loads(raw.group(1))
    assert [step["kind"] for step in lesson["steps"]] == ["meet"] * 4 + ["find"] * 4
    assert lesson["practice"] is False
    assert lesson["mic"] is False, "only where the parent let the child speak"
    assert lesson["older"] is False
    assert lesson["strings"]["find"] == "Hangisi? Dinle ve dokun."
    assert "/static/english.js" in page


def test_after_the_lesson_more_is_practice_that_moves_no_box(
    client: TestClient, with_english: FakeShellDaemon
) -> None:
    import json
    import re

    with_english.english = {
        "today": "2026-10-22",
        "items": {"animals/cat": [2, "2026-10-25", "2026-10-20"]},
    }
    page = client.get("/english/lesson?more=1").text
    raw = re.search(r'id="lesson-data">(.*?)</script>', page, re.S)
    assert raw is not None
    lesson = json.loads(raw.group(1))
    assert lesson["practice"] is True
    assert [step["item"] for step in lesson["steps"]] == ["animals/cat"]


def test_an_answer_goes_to_the_daemon(
    client: TestClient, with_english: FakeShellDaemon
) -> None:
    reply = _json_post(client, "/english/answer", {"item": "animals/cat", "right": True})
    assert reply.json() == {"ok": True, "box": 1}
    assert with_english.english_answers == [("animals/cat", True)]


def test_a_website_cannot_post_an_answer(
    client: TestClient, with_english: FakeShellDaemon
) -> None:
    reply = client.post(
        "/english/answer",
        json={"item": "animals/cat", "right": True},
        headers={"origin": "https://example.com", "sec-fetch-site": "cross-site"},
    )
    assert reply.status_code == 403
    assert with_english.english_answers == []


def test_english_is_said_by_the_english_voice_and_the_rest_in_the_childs_language(
    client: TestClient, with_english: FakeShellDaemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    said: list[tuple[str, str]] = []

    async def say_and_wait(text: str, locale: str) -> bool:
        said.append((text, locale))
        return True

    monkeypatch.setattr(client.app.state.shell, "say_and_wait", say_and_wait)  # type: ignore[attr-defined]
    assert _json_post(client, "/english/say", {"text": "cat", "lang": "en"}).json() == {
        "ok": True
    }
    _json_post(client, "/english/say", {"text": "Yeni bir kelime!", "lang": "ui"})
    assert said == [("cat", "en"), ("Yeni bir kelime!", "tr")]


def test_a_word_said_back_is_heard_as_english_and_compared(
    client: TestClient, with_english: FakeShellDaemon
) -> None:
    wav = b"RIFF" + b"\0" * 64
    headers = {"origin": LAUNCHER, "content-type": "audio/wav", "sec-fetch-site": "same-origin"}
    reply = client.post("/english/hear?item=animals/cat", content=wav, headers=headers).json()
    assert reply == {"ok": True, "heard": "a cat", "match": True}
    assert with_english.heard == ["en"]
    with_english.hear_text = "dog"
    reply = client.post("/english/hear?item=animals/cat", content=wav, headers=headers).json()
    assert reply["match"] is False
    bad = client.post("/english/hear?item=animals/unicorn", content=wav, headers=headers)
    assert bad.status_code == 400


def test_the_lesson_minutes_are_the_corners_in_its_kind_of_app(
    client: TestClient, with_english: FakeShellDaemon
) -> None:
    assert _json_post(client, "/english/active", {}).json() == {"ok": True}
    # Its kind - learning, unless the parent chose otherwise - is the daemon's.
    assert with_english.activity == [("english", "İngilizce", "")]


def test_my_words_shows_the_met_ones_and_keeps_the_rest_for_later(
    client: TestClient, with_english: FakeShellDaemon
) -> None:
    with_english.english = {
        "today": "2026-10-22",
        "items": {
            "animals/cat": [5, "2026-11-20", "2026-10-01"],
            "animals/dog": [1, "2026-10-23", "2026-10-22"],
        },
    }
    page = client.get("/english/words").text
    assert 'data-say-en="cat"' in page and 'data-say-en="dog"' in page
    assert 'data-say-en="bird"' not in page
    assert page.count("word--known") == 1
    assert page.count("word--later") == 96


def test_without_the_daemon_a_lesson_still_starts_from_the_beginning(
    client: TestClient, with_english: FakeShellDaemon
) -> None:
    with_english.english = None
    assert 'href="/english/lesson"' in client.get("/english").text


# ------------------------------------------------------ English with Sio (D-226)


def test_the_chat_is_there_only_where_the_assistant_is_on(
    client: TestClient, with_english: FakeShellDaemon
) -> None:
    assert with_english.data is not None
    assert "/english/chat" not in client.get("/english").text
    response = client.get("/english/chat", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/english"

    with_english.data["assistant"] = {"enabled": True, "voice": False}
    assert 'href="/english/chat"' in client.get("/english").text
    page = client.get("/english/chat").text
    import json
    import re

    raw = re.search(r'id="assistant-mode" type="application/json">(.*?)</script>', page, re.S)
    assert raw is not None
    chat = json.loads(raw.group(1))
    assert chat["mode"] == "english"
    assert chat["opening"] == (
        "Hello, Elif! I'm Sio. Let's talk in English! What is your favourite animal?"
    ), "about the unit being learned"
    assert 'data-chip="Yes"' in page and 'data-chip="I don&#39;t know"' in page
    assert "assistant-mic" not in page, "no microphone unless the parent said so"
    assert "/static/assistant.js" in page


def test_a_turn_of_english_carries_the_conversation_and_the_known_words(
    client: TestClient, with_english: FakeShellDaemon
) -> None:
    with_english.english = {
        "today": "2026-10-22",
        "items": {"animals/cat": [2, "2026-10-24", "2026-10-20"]},
    }
    history = [["sio", "What is your favourite animal?"], ["child", "cat"], ["x", "dropped"]]
    reply = _json_post(
        client, "/assistant/ask", {"question": "dog", "mode": "english", "history": history}
    )
    assert reply.status_code == 200
    [(question, chat)] = with_english.chat_asked
    assert question == "dog"
    assert chat["mode"] == "english"
    assert chat["history"] == history[:2]
    assert chat["words"] == ["cat"]


def test_sios_sentences_are_said_in_english_in_his_voice(
    client: TestClient, with_english: FakeShellDaemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    said: list[tuple[str, str, str]] = []

    async def say_and_wait(text: str, locale: str, persona: str = "zio") -> bool:
        said.append((text, locale, persona))
        return True

    monkeypatch.setattr(client.app.state.shell, "say_and_wait", say_and_wait)  # type: ignore[attr-defined]
    _json_post(client, "/assistant/say", {"text": "A dog!", "mode": "english"})
    _json_post(client, "/assistant/say", {"text": "Gökyüzü mavidir."})
    # Zio's answer to a question asked in English, in English (D-244).
    _json_post(client, "/assistant/say", {"text": "The sky is blue.", "lang": "en"})
    _json_post(client, "/assistant/say", {"text": "Merhaba.", "lang": "de"})
    assert said == [
        ("A dog!", "en", "sio"),
        ("Gökyüzü mavidir.", "tr", "zio"),
        ("The sky is blue.", "en", "zio"),
        ("Merhaba.", "tr", "zio"),
    ]


def test_talking_with_sio_is_heard_as_english(
    client: TestClient, with_english: FakeShellDaemon
) -> None:
    wav = b"RIFF" + b"\0" * 64
    headers = {"origin": LAUNCHER, "content-type": "audio/wav", "sec-fetch-site": "same-origin"}
    client.post("/assistant/hear?lang=en", content=wav, headers=headers)
    client.post("/assistant/hear", content=wav, headers=headers)
    # A question to Zio: Turkish or English, whichever the child spoke (D-244).
    assert with_english.heard == ["en", "auto"]


# ---------------------------------------------------------- D-227 switch user


def test_the_home_screen_offers_switch_user_and_has_the_cover(client: TestClient) -> None:
    page = client.get("/").text
    assert 'href="/switch-user"' in page
    assert 'id="app-cover"' in page and 'id="app-cover-home"' in page
    assert re.search(r'id="app-cover"\s+hidden', page), "covered only while an app is in front"
    assert 'data-kind="app"' in page, "a re-tap asks again only for an app"


def test_switch_user_asks_first_then_says_goodbye(
    client: TestClient, daemon: FakeShellDaemon
) -> None:
    page = client.get("/switch-user").text
    assert 'action="/switch-user"' in page
    assert "Başkası mı girecek?" in _visible_text(page)
    assert daemon.switch_calls == 0

    page = client.post("/switch-user").text
    text = _visible_text(page)
    assert daemon.switch_calls == 1
    assert "Giriş ekranı geliyor" in text
    assert "Merhaba Elif" not in text, "never the home screen"
    assert 'data-back-after="60"' in page


def test_a_switch_the_daemon_never_heard_is_said_so(
    client: TestClient, daemon: FakeShellDaemon
) -> None:
    daemon.switch_answer = False
    page = client.post("/switch-user").text
    text = _visible_text(page)
    assert "Oturum değişmedi" in text
    assert "Bir büyüğüne söyle" in text
    assert 'action="/switch-user"' not in page


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ({"ok": True}, True),
        ({"ok": False, "error": "shell.not_allowed"}, False),
    ],
)
async def test_the_client_says_whether_the_switch_was_taken(
    tmp_path: pathlib.Path, answer: dict[str, Any], expected: bool
) -> None:
    from biokids_core.ipc import Response
    from biokids_shell.client import ShellClient

    client = ShellClient(tmp_path / "shell.sock")
    client._client = _AnsweringClient(Response(**answer))  # type: ignore[assignment]
    assert await client.switch_user() is expected
    client._client = _AnsweringClient(IpcError("gone"))  # type: ignore[assignment]
    assert await client.switch_user() is False


# ------------------------------------------------------ the last round (D-230)


def test_the_last_round_is_asked_of_the_daemon(
    client: TestClient, daemon: FakeShellDaemon
) -> None:
    daemon.finish_up_answer = 180
    response = client.post("/finish-up", json={})
    assert response.json() == {"ok": True, "minutes": 3}
    assert daemon.finish_up_calls == 1


def test_a_last_round_not_due_says_no(client: TestClient, daemon: FakeShellDaemon) -> None:
    assert client.post("/finish-up", json={}).json() == {"ok": False, "minutes": 0}


def test_the_last_round_from_the_footer_goes_home(
    client: TestClient, daemon: FakeShellDaemon
) -> None:
    response = client.post("/finish-up", data={"go": "1"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert daemon.finish_up_calls == 1


def test_another_site_cannot_ask_for_the_last_round(
    client: TestClient, daemon: FakeShellDaemon
) -> None:
    response = client.post("/finish-up", json={}, headers={"Origin": "https://example.com"})
    assert response.status_code == 403
    assert daemon.finish_up_calls == 0


def test_the_footer_offers_the_last_round_only_when_due(
    client: TestClient, daemon: FakeShellDaemon
) -> None:
    assert 'action="/finish-up"' not in client.get("/").text
    assert daemon.data is not None
    daemon.data["finish_up"] = {"offer_seconds": 180, "used_seconds": 0}
    page = client.get("/").text
    assert 'action="/finish-up"' in page
    assert "+3 dk" in page
    # The warning's own button is always in the page, hidden until offered.
    assert 'id="notice-finish"' in page


# ------------------------------------------------ games after learning (D-232)


def test_game_tiles_say_how_much_learning_is_left(
    client: TestClient, daemon: FakeShellDaemon
) -> None:
    assert daemon.data is not None
    daemon.data["tiles"].append(
        {
            "slug": "supertux",
            "name": "SuperTux",
            "icon": "game",
            "kind": "app",
            "url": "",
            "exec": "supertux2",
            "category": "games",
        }
    )
    daemon.data["learn_first"] = {"minutes": 15, "left_seconds": 9 * 60 + 1}
    page = client.get("/").text
    assert page.count("tile--waiting") == 1, "the game, not the learning or free tiles"
    assert "10 dk öğrenince" in page

    daemon.data["learn_first"] = {"minutes": 15, "left_seconds": 0}
    assert "tile--waiting" not in client.get("/").text


def test_the_calm_screen_for_learning_first_shows_the_way(
    client: TestClient, daemon: FakeShellDaemon
) -> None:
    assert daemon.data is not None
    daemon.data["learn_first"] = {"minutes": 15, "left_seconds": 5 * 60}
    page = client.get("/blocked?reason=games.learn_first&slug=supertux").text
    assert "Önce biraz öğrenelim" in page
    assert "15 dakika öğrendikten sonra" in page and "5 dakika kaldı" in page
    assert 'class="learn-bar"' in page
    assert "width: 67%" in page
    assert "Büyüğüme sor" not in page, "a rule about order is not asked away"


# ------------------------------------------------------ Birlikte modu (D-235)


def test_the_home_screen_says_a_grown_up_is_here(
    client: TestClient, daemon: FakeShellDaemon
) -> None:
    assert "together-chip" not in client.get("/").text
    assert daemon.data is not None
    daemon.data["together_seconds"] = 20 * 60
    page = client.get("/").text
    assert "together-chip" in page and "20 dk" in page


# ---------------------------------------------------------- a birthday (D-236)


def test_a_birthday_is_shown_all_day_and_said_once(
    client: TestClient, daemon: FakeShellDaemon, tmp_path: Path
) -> None:
    client.app.state.shell.home = tmp_path  # type: ignore[attr-defined]
    assert "birthday__cake" not in client.get("/").text
    assert daemon.data is not None
    daemon.data["birthday"] = True
    first = client.get("/").text
    assert "İyi ki doğdun Elif!" in first and "data-birthday-say" in first
    again = client.get("/").text
    assert "İyi ki doğdun Elif!" in again and "data-birthday-say" not in again


def test_the_brightness_slider_starts_where_the_screen_is(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from biokids_shell import device

    async def sixty() -> int:
        return 60

    monkeypatch.setattr(device, "get_brightness", sixty)
    assert 'id="brightness" min="20" max="100" step="5" value="60"' in client.get("/").text


# ----------------------------------------------------- the review (D-229..D-241)


def test_the_launcher_stays_quiet_when_zio_is_off(
    client: TestClient, daemon: FakeShellDaemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    said: list[str] = []

    async def say(text: str, locale: str) -> None:
        said.append(text)

    monkeypatch.setattr(client.app.state.shell, "say", say)  # type: ignore[attr-defined]
    assert client.post("/speak-label", json={"text": "Merhaba"}).json() == {"ok": True}
    assert daemon.data is not None
    daemon.data["voice_enabled"] = False
    assert client.post("/speak-label", json={"text": "Merhaba"}).json() == {"ok": False}
    assert said == ["Merhaba"]


def test_a_stopped_app_is_said_once_not_twice(client: TestClient) -> None:
    """Zio says why the game went (D-229); the calm screen it lands on is quiet."""
    quiet = client.get("/blocked?reason=category.exhausted&slug=supertux&said=1").text
    assert "data-speak-on-load" not in quiet
    spoken = client.get("/blocked?reason=category.exhausted&slug=supertux").text
    assert "data-speak-on-load" in spoken


def test_another_site_cannot_post_the_last_round_form(
    client: TestClient, daemon: FakeShellDaemon
) -> None:
    response = client.post(
        "/finish-up",
        data={"go": "1"},
        headers={"Origin": "https://example.com", "Sec-Fetch-Site": "cross-site"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert daemon.finish_up_calls == 0


# --------------------------------------------- D-245 the logo, press-and-speak


def test_the_launcher_shows_the_logo_and_its_tab_the_kite(client: TestClient) -> None:
    page = client.get("/").text
    assert 'class="hello__logo" src="/static/shared/brand/logo.png' in page
    assert '<link rel="icon" href="/favicon.ico' in page
    assert client.get("/favicon.ico").content[:4] == b"\x00\x00\x01\x00"


def test_press_and_speak_shows_the_voice_and_the_words_to_check(
    client: TestClient, with_english: FakeShellDaemon
) -> None:
    assert with_english.data is not None
    with_english.data["assistant"] = {"enabled": True, "voice": True}
    for path in ("/assistant", "/english/chat"):
        page = client.get(path).text
        assert 'id="assistant-mic"' in page, path
        assert 'id="assistant-level"' in page and page.count("<i></i>") == 9, path
        assert 'id="assistant-draft" hidden' in page, path
        assert "Sesimi dinle" in page and "assistant-voice-strings" in page, path
    with_english.data["assistant"] = {"enabled": True, "voice": False}
    page = client.get("/assistant").text
    assert 'id="assistant-mic"' not in page and 'id="assistant-draft"' not in page


def test_the_microphone_is_held_with_the_pointer_and_the_words_wait_for_send() -> None:
    script = (STATIC / "assistant.js").read_text(encoding="utf-8")
    assert "setPointerCapture" in script, "a finger sliding off the button does not stop it"
    assert '"pointerleave"' not in script
    assert "if (!holding)" in script, "let go before the microphone opened: nothing records"
    assert "input.value = heard.text" in script and "ask(heard.text)" not in script
    assert "getFloatTimeDomainData" in script, "the bars move with the voice"


def test_a_bubble_tapped_is_read_again_and_kapat_carries_the_kite(client: TestClient) -> None:
    """D-245: a child who missed a sentence taps it; Kapat has the logo, as a
    computer's own off button carries its maker's."""
    script = (STATIC / "assistant.js").read_text(encoding="utf-8")
    assert 'log.addEventListener("click"' in script and "is-replaying" in script
    page = client.get("/").text
    assert 'class="tile-small__kite" src="/static/shared/brand/kite.png' in page


def test_switching_off_the_kite_flies_away(client: TestClient, daemon: FakeShellDaemon) -> None:
    """D-246: the kite on Kapat flies off the goodbye screen; not on a switch of user."""
    page = client.post("/shutdown").text
    assert 'class="goodbye-kite"' in page
    assert 'class="goodbye-kite"' not in client.post("/switch-user").text
    css = (STATIC / "shell.css").read_text(encoding="utf-8")
    assert "@keyframes kite-fly" in css and ".goodbye-kite { animation: none;" in css


def test_a_kiosk_on_two_monitors_is_fitted_once_not_every_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-254: the X screen spans both monitors and the fullscreen kiosk one."""
    from biokids_shell import kiosk

    monkeypatch.setattr(kiosk, "FIT_CHECK_SECONDS", 0.001)
    ran: list[tuple[str, ...]] = []
    stop = asyncio.Event()
    checks = [0]

    async def fake(*args: str) -> str | None:
        ran.append(args)
        if args[0] == "getdisplaygeometry":
            return "3840 1080\n"
        if args[0] == "search":
            return "9\n"
        if args[0] == "getwindowgeometry":
            checks[0] += 1
            if checks[0] == 5:
                stop.set()
            return "Window 9\n  Position: 0,0 (screen: 0)\n  Geometry: 1920x1080\n"
        return ""

    asyncio.run(kiosk.keep_fullscreen(stop, fake))
    assert sum(call[0] == "windowsize" for call in ran) == 1


def test_a_brightness_drag_is_applied_one_at_a_time_and_the_last_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-254: a dozen xrandr runs a second held the X server, and every tap."""
    from biokids_shell import device

    applied: list[list[str]] = []

    async def run(argv: list[str]) -> bool:
        if argv[0] == "brightnessctl":
            return False  # a desktop: no backlight
        applied.append(argv)
        await asyncio.sleep(0.01)
        return True

    async def output() -> str:
        return "HDMI-1"

    monkeypatch.setattr(device, "_run", run)
    monkeypatch.setattr(device, "_primary_output", output)
    monkeypatch.setattr(device, "_brightness_method", "")
    monkeypatch.setattr(device, "_brightness_output", None)

    async def drag() -> list[bool]:
        return await asyncio.gather(*(device.set_brightness(v) for v in range(20, 101, 5)))

    assert all(asyncio.run(drag()))
    levels = [argv[-1] for argv in applied]
    assert levels[0] == "0.20" and levels[-1] == "1.00"
    assert len(levels) <= 2, f"one change at a time, the latest: {levels}"
    assert all("--current" in argv for argv in applied), "never a probe of the monitors"
