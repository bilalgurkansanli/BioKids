"""D-217 - text size, high contrast and a reading font.

A parent chooses them for themselves under Ayarlar → Görünüm, kept with their
own account; and for each child under Çocuklar → Düzenle → Ekran, carried by the
daemon to the launcher. Either way they end as three attributes on <html>. What
is worth asserting: that a choice is stored where it says and reaches the page,
that nothing a form or the daemon sends can put anything else in an attribute,
that the high-contrast colours really are high contrast, and that the young
band's grid still fits a 1280-pixel screen at the largest size.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from biokids_core import appearance, repo, security
from biokids_core.appearance import Appearance
from biokids_core.config import Config
from biokids_core.db import AuditLog, Child, Database, Parent
from biokids_core.db.models import utcnow
from biokids_panel.app import create_app
from biokids_shell import app as shell_app
from test_panel import PASSPHRASE, PIN, FakeDaemon
from test_shell import LAUNCHER, FakeShellDaemon, a_state

ROOT = Path(__file__).resolve().parent.parent
TOKENS = ROOT / "src" / "biokids_core" / "data" / "static" / "tokens.css"
SHELL_STATIC = ROOT / "src" / "biokids_shell" / "static"
PANEL_CSS = ROOT / "src" / "biokids_panel" / "static" / "panel.css"
BABA_PIN = "551234"


# ------------------------------------------------------------- the values


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            {"text_size": "x-large", "high_contrast": True, "reading_font": True},
            Appearance("x-large", True, True),
        ),
        ({"text_size": "huge", "high_contrast": "yes", "reading_font": "1"}, Appearance()),
        ({"text_size": '"><script>', "high_contrast": None}, Appearance()),
        ("x-large", Appearance()),
        (None, Appearance()),
    ],
)
def test_only_known_values_survive(raw: Any, expected: Appearance) -> None:
    assert Appearance.from_state(raw) == expected


def test_the_attributes_are_the_ones_tokens_css_reads() -> None:
    chosen = Appearance("large", True, True)
    assert (chosen.text_size, chosen.contrast, chosen.font) == ("large", "high", "dyslexic")
    assert (Appearance().contrast, Appearance().font) == ("normal", "default")
    assert Appearance.from_state(chosen.as_dict()) == chosen


def test_the_reading_font_is_found_where_debian_puts_it(tmp_path: Path) -> None:
    font_dir = tmp_path / "opentype" / "opendyslexic"
    assert not appearance.reading_font_installed([font_dir])
    font_dir.mkdir(parents=True)
    (font_dir / "OpenDyslexic-Regular.otf").write_bytes(b"")
    assert appearance.reading_font_installed([tmp_path / "nowhere", font_dir])
    assert Path("/usr/share/fonts/opentype/opendyslexic") in appearance.READING_FONT_DIRS


# ------------------------------------------------------------- the panel


@pytest.fixture
def daemon() -> FakeDaemon:
    return FakeDaemon()


@pytest.fixture
def client(config: Config, db: Database, daemon: FakeDaemon) -> TestClient:
    app = create_app(config)
    app.state.ctx.client = daemon  # type: ignore[assignment]
    return TestClient(app)


@pytest.fixture
def household(db: Database) -> None:
    from biokids_core.db import Policy

    with db.session() as session:
        repo.get_or_create_family(session).setup_completed_at = utcnow()
        session.add(
            Parent(
                display_name="Anne",
                pin_hash=security.hash_secret(PIN),
                passphrase_hash=security.hash_secret(PASSPHRASE),
            )
        )
        session.add(Parent(display_name="Baba", pin_hash=security.hash_secret(BABA_PIN)))
        child = Child(unix_user="elif", display_name="Elif", birth_year=2020)
        session.add(child)
        session.flush()
        session.add(Policy(child_id=child.id))


def sign_in(client: TestClient, pin: str, parent: int) -> None:
    response = client.post(
        "/login", data={"pin": pin, "parent": str(parent)}, follow_redirects=False
    )
    assert response.status_code == 303


def csrf_of(client: TestClient, page: str = "/settings") -> str:
    found = re.search(r'name="csrf_token" value="([^"]+)"', client.get(page).text)
    assert found is not None
    return found.group(1)


def html_tag(page: str) -> str:
    found = re.search(r"<html[^>]*>", page)
    assert found is not None
    return found.group(0)


def test_a_parents_appearance_is_kept_with_their_account(
    client: TestClient, household: None, db: Database
) -> None:
    sign_in(client, PIN, 1)
    assert 'data-text-size="normal"' in html_tag(client.get("/today").text)
    response = client.post(
        "/settings/appearance",
        data={"text_size": "x-large", "high_contrast": "on", "csrf_token": csrf_of(client)},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/settings?saved=1#appearance"
    with db.session() as session:
        anne = session.get(Parent, 1)
        assert anne is not None and (anne.text_size, anne.high_contrast) == ("x-large", True)

    for page in ("/today", "/children", "/settings", "/audit"):
        tag = html_tag(client.get(page).text)
        assert 'data-text-size="x-large"' in tag and 'data-contrast="high"' in tag, page

    phone = TestClient(client.app)
    sign_in(phone, PIN, 1)
    assert 'data-text-size="x-large"' in html_tag(phone.get("/today").text), "on the phone too"

    other = TestClient(client.app)
    sign_in(other, BABA_PIN, 2)
    tag = html_tag(other.get("/today").text)
    assert 'data-text-size="normal"' in tag and 'data-contrast="normal"' in tag, "not Baba's"

    signed_out = TestClient(client.app).get("/login").text
    assert 'data-text-size="normal"' in html_tag(signed_out)

    with db.session() as session:
        (row,) = session.query(AuditLog).filter(AuditLog.action == "appearance_changed").all()
    assert row.actor == "parent:1"
    assert row.detail["after"] == {
        "text_size": "x-large",
        "high_contrast": True,
        "reading_font": False,
    }


def test_the_settings_show_the_choice_and_refuse_nonsense(
    client: TestClient, household: None, db: Database
) -> None:
    sign_in(client, PIN, 1)
    client.post(
        "/settings/appearance",
        data={"text_size": "large", "csrf_token": csrf_of(client)},
    )
    page = client.get("/settings").text
    assert re.search(r'value="large"\s+checked', page)
    client.post(
        "/settings/appearance",
        data={"text_size": "enormous", "high_contrast": "on", "csrf_token": csrf_of(client)},
    )
    with db.session() as session:
        anne = session.get(Parent, 1)
        assert anne is not None and (anne.text_size, anne.high_contrast) == ("normal", True)

    forged = client.post(
        "/settings/appearance", data={"text_size": "x-large"}, follow_redirects=False
    )
    assert "error=csrf" in forged.headers["location"]


def test_a_childs_screen_is_set_on_their_page(
    client: TestClient,
    household: None,
    db: Database,
    daemon: FakeDaemon,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from biokids_panel.routes import children

    monkeypatch.setattr(children, "reading_font_installed", lambda: True)
    sign_in(client, PIN, 1)
    page = client.get("/children/1/edit").text
    assert "Okuma kolaylığı yazı tipi" in page and "OpenDyslexic" in page
    assert 'id="screen" >' in page or 'id="screen">' in page, "folded away while unset"

    form = {
        "display_name": "Elif",
        "birth_year": "2020",
        "screen": "1",
        "text_size": "x-large",
        "high_contrast": "on",
        "reading_font": "on",
        "csrf_token": csrf_of(client, "/children/1/edit"),
    }
    response = client.post("/children/1/edit", data=form, follow_redirects=False)
    assert response.headers["location"] == "/children/1/edit?saved=1"
    assert daemon.ran("reload"), "the launcher hears of it through the daemon"
    with db.session() as session:
        elif_ = session.get(Child, 1)
        assert elif_ is not None
        assert (elif_.text_size, elif_.high_contrast, elif_.reading_font) == (
            "x-large",
            True,
            True,
        )
        (row,) = session.query(AuditLog).filter(AuditLog.action == "child_changed").all()
    assert row.detail["before"]["text_size"] == "normal"
    assert row.detail["after"]["reading_font"] is True
    assert 'id="screen" open' in client.get("/children/1/edit").text

    # A form without the Ekran part - an older page, a script - changes none of it.
    client.post(
        "/children/1/edit",
        data={k: v for k, v in form.items() if k not in {"screen", "high_contrast"}},
    )
    with db.session() as session:
        elif_ = session.get(Child, 1)
        assert elif_ is not None and elif_.high_contrast is True


def test_the_panel_says_when_the_reading_font_is_not_installed(
    client: TestClient, household: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from biokids_panel.routes import children

    monkeypatch.setattr(children, "reading_font_installed", lambda: False)
    sign_in(client, PIN, 1)
    page = client.get("/children/1/edit").text
    assert "bu bilgisayarda kurulu değil" in page and "fonts-opendyslexic" in page


# ------------------------------------------------------------ the daemon


async def test_the_daemon_hands_the_childs_screen_to_the_launcher(
    db: Database, config: Config, child: Child, session: Any
) -> None:
    from biokids_core.clock import FakeClock
    from biokids_daemon.bus import EventBus
    from biokids_daemon.enforcer import Enforcer, RecordingRunner
    from biokids_daemon.sessions import FakeSessionSource, SessionWatcher
    from biokids_daemon.supervisor import Supervisor

    supervisor = Supervisor(
        database=db,
        bus=EventBus(),
        watcher=SessionWatcher(FakeSessionSource()),
        enforcer=Enforcer(config, RecordingRunner()),
        clock=FakeClock(wall=dt.datetime(2026, 9, 21, 16, 0, 0)),
        config=config,
    )
    await supervisor.start()
    try:
        assert supervisor.shell_state(child.id)["appearance"] == Appearance().as_dict()
        row = session.get(Child, child.id)
        row.text_size, row.high_contrast, row.reading_font = "large", True, False
        session.commit()
        supervisor.reload()
        assert supervisor.shell_state(child.id)["appearance"] == {
            "text_size": "large",
            "high_contrast": True,
            "reading_font": False,
        }
    finally:
        await supervisor.stop()


# ----------------------------------------------------------- the launcher


@pytest.fixture
def launcher(config: Config) -> tuple[TestClient, FakeShellDaemon]:
    daemon = FakeShellDaemon(a_state())
    app = shell_app.create_app(config)
    app.state.shell.client = daemon  # type: ignore[assignment]
    return TestClient(app, base_url=LAUNCHER), daemon


def test_the_launcher_draws_the_childs_screen(
    launcher: tuple[TestClient, FakeShellDaemon],
) -> None:
    client, daemon = launcher
    tag = html_tag(client.get("/").text)
    assert 'data-text-size="normal"' in tag and 'data-contrast="normal"' in tag
    assert 'data-font="default"' in tag

    assert daemon.data is not None
    daemon.data["appearance"] = {
        "text_size": "x-large",
        "high_contrast": True,
        "reading_font": True,
    }
    for page in ("/", "/start", "/blocked?url=https://example.com", "/shutdown"):
        tag = html_tag(client.get(page).text)
        assert 'data-text-size="x-large"' in tag, page
        assert 'data-contrast="high"' in tag and 'data-font="dyslexic"' in tag, page
    assert "/static/display.css?v=" in client.get("/").text


def test_nothing_the_state_says_reaches_an_attribute_unchecked(
    launcher: tuple[TestClient, FakeShellDaemon],
) -> None:
    client, daemon = launcher
    assert daemon.data is not None
    daemon.data["appearance"] = {
        "text_size": '"><script>alert(1)</script>',
        "high_contrast": "on",
    }
    page = client.get("/").text
    assert "<script>alert" not in page
    assert 'data-text-size="normal"' in html_tag(page)


def test_a_launcher_without_its_daemon_is_drawn_plainly(config: Config) -> None:
    app = shell_app.create_app(config)
    app.state.shell.client = FakeShellDaemon(None)  # type: ignore[assignment]
    page = TestClient(app, base_url=LAUNCHER).get("/").text
    assert 'data-text-size="normal"' in html_tag(page)


# ---------------------------------------------------------- the stylesheets


def _rule(css: str, selector: str) -> str:
    """The declarations of every rule that names `selector`, in order: the
    cascade's view of it, as far as a page without media queries goes."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    found = [
        match.group(2)
        for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", css)
        if selector in [s.strip() for s in match.group(1).split(",")]
    ]
    assert found, f"no rule for {selector}"
    return "\n".join(found)


def _colours(block: str) -> dict[str, str]:
    return dict(re.findall(r"(--[\w-]+):\s*(#[0-9A-Fa-f]{6})", block))


def _luminance(colour: str) -> float:
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (int(colour[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(a: str, b: str) -> float:
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_every_text_size_has_its_scale() -> None:
    tokens = TOKENS.read_text(encoding="utf-8")
    scales = {
        size: float(
            re.search(
                r"--font-scale:\s*([\d.]+)", _rule(tokens, f'html[data-text-size="{size}"]')
            )[1]
        )  # type: ignore[index]
        for size in appearance.TEXT_SIZES
        if size != "normal"
    }
    assert scales == {"large": 1.25, "x-large": 1.5}
    assert "--font-scale: 1;" in _rule(tokens, ":root")


def test_high_contrast_is_wcag_aa_or_better() -> None:
    tokens = TOKENS.read_text(encoding="utf-8")
    base = _colours(_rule(tokens, ":root"))
    high = {**base, **_colours(_rule(tokens, 'html[data-contrast="high"]'))}
    white = "#FFFFFF"

    # Text: AAA for the words themselves, which the launcher promises (UX.md 5).
    assert contrast(high["--ink"], high["--bg"]) >= 7
    assert contrast(high["--ink-soft"], high["--surface"]) >= 7
    # White words on the coloured buttons and banners: AA at least.
    for token in ("--ok", "--danger", "--warn", "--bio-indigo"):
        assert contrast(white, high[token]) >= 4.5, token
    # Links are drawn in indigo.
    assert contrast(high["--bio-indigo"], high["--surface"]) >= 4.5
    # Lines, rings, bars and borders: 3:1, WCAG's non-text contrast.
    for token in ("--line", "--ok", "--warn", "--danger", "--bio-teal", "--bio-coral"):
        assert contrast(high[token], high["--surface"]) >= 3, token
    # The amber banner keeps its dark words.
    assert contrast("#3A2A00", high["--bio-amber"]) >= 4.5
    # And it is higher than normal where it matters most.
    assert contrast(high["--ink-soft"], high["--bg"]) > contrast(
        base["--ink-soft"], base["--bg"]
    )


def test_the_warning_banner_turns_white_in_high_contrast() -> None:
    display = (SHELL_STATIC / "display.css").read_text(encoding="utf-8")
    rule = _rule(display, 'html[data-contrast="high"] .banner[data-level="warn"]')
    assert "#FFFFFF" in rule


def test_the_reading_font_is_the_installed_one() -> None:
    """No @font-face for it, which would hide the installed font behind a file
    that is not there, and no stack that names itself, which is no value at
    all: the launcher fell back to the browser's serif."""
    tokens = TOKENS.read_text(encoding="utf-8")
    assert "OpenDyslexic-Regular.woff2" not in tokens
    block = _rule(tokens, 'html[data-font="dyslexic"]')
    for name in ("--font-ui", "--font-kid"):
        value = re.search(rf"{name}:\s*([^;]+);", block)
        assert value is not None and value[1].startswith('"OpenDyslexic"')
        assert f"var({name})" not in value[1]
        assert value[1].rstrip().endswith("sans-serif")


def test_the_launcher_scales_its_text_once() -> None:
    shell = (SHELL_STATIC / "shell.css").read_text(encoding="utf-8")
    daily = (SHELL_STATIC / "daily.css").read_text(encoding="utf-8")
    assert "calc(100% * var(--font-scale))" in _rule(shell, "html")
    assert "font-size: 1rem" in _rule(shell, "body")
    for css in (shell, daily):
        assert not re.search(r"calc\([\d.]+rem \* var\(--font-scale\)\)", css), (
            "a rem is already scaled at the root"
        )


def _px(css: str, name: str) -> int:
    found = re.search(rf"{re.escape(name)}:\s*(\d+)px", css)
    assert found is not None, name
    return int(found.group(1))


def _per_row(screen: int, tile: float, gap: int, side: int, scrollbar: int = 16) -> int:
    return int((screen - 2 * side - scrollbar + gap) // (tile + gap))


def test_the_young_grid_still_fits_1280_at_the_largest_size() -> None:
    """Measured in Chromium at 1280 x 800 (the runbook says how): four tiles a
    row instead of five, the grid scrolling inside itself, nothing wider than the
    screen, and the longest single word a tile carries ("Kaplumbağa") inside it."""
    shell = (SHELL_STATIC / "shell.css").read_text(encoding="utf-8")
    display = (SHELL_STATIC / "display.css").read_text(encoding="utf-8")
    tokens = TOKENS.read_text(encoding="utf-8")
    young = _rule(shell, '.screen[data-band="4-7"]')
    tile, label = _px(young, "--tile-size"), _px(young, "--tile-label")
    grow = float(
        re.search(r"--tile-grow:\s*([\d.]+)", _rule(display, 'html[data-text-size="x-large"]'))[
            1
        ]
    )  # type: ignore[index]
    gap, side, pad = _px(tokens, "--sp-4"), _px(tokens, "--sp-5"), _px(tokens, "--sp-3")

    normal = _per_row(1280, tile, gap, side)
    largest = _per_row(1280, tile * grow, gap, side)
    assert 3 <= largest <= normal
    assert largest * tile * grow + (largest - 1) * gap <= 1280 - 2 * side, "nothing overflows"

    sized = _rule(display, 'html[data-text-size="x-large"] .tile')
    assert "height: auto" in sized and "min-height" in sized, "a long name grows the tile"
    words = _rule(display, 'html[data-text-size="x-large"] .tile .tile__label')
    assert "var(--font-scale)" in words and "overflow-wrap: anywhere" in words
    # "Kaplumbağa": ten letters of Fredoka at about 0.55 em each.
    assert 10 * 0.55 * label * 1.5 <= tile * grow - 2 * pad


def test_the_phone_menu_keeps_six_entries_at_a_larger_size() -> None:
    css = PANEL_CSS.read_text(encoding="utf-8")
    narrow = re.search(r"@media \(max-width: 39\.99rem\) \{(.*?)\n\}", css, flags=re.DOTALL)
    assert narrow is not None
    assert 'html[data-text-size="x-large"] .nav a' in narrow.group(1)
    assert "min-width: 0" in narrow.group(1)
    assert "flex-wrap: wrap" in _rule(css, ".topbar")
