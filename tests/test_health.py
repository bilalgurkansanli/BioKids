"""D-237: Koruma durumu - whether everything that protects the child is in place.

Each check is read from the machine or from the daemon's status. What matters:
green only when it is really so, the daemon being down says so instead of
guessing, the parent's to-do comes first, and the panel shows it.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import test_panel
from biokids_core import health
from biokids_core.browser_policy import POLICY_FILENAME
from biokids_core.config import Config
from biokids_core.health import Level
from test_panel import FakeDaemon, login

client = test_panel.client
daemon = test_panel.daemon
family_ready = test_panel.family_ready

GOOD_STATUS: dict[str, Any] = {"children": [], "dns_filter": {"running": True, "open": False}}
NOW = dt.datetime(2026, 10, 23, 12, 0)


def _levels(found: list[health.Check]) -> dict[str, Level]:
    return {check.key: check.level for check in found}


def _everything_in_place(config: Config, tmp_path: Path) -> dict[str, Any]:
    config.paths.chromium_policy_dir.mkdir(parents=True, exist_ok=True)
    (config.paths.chromium_policy_dir / POLICY_FILENAME).write_text("{}", encoding="utf-8")
    config.paths.extension_dir.mkdir(parents=True, exist_ok=True)
    from biokids_core.browser_policy import EXTENSION_ID_FILE

    (config.paths.extension_dir / EXTENSION_ID_FILE).write_text("a" * 32, encoding="ascii")
    config.paths.data_dir.mkdir(parents=True, exist_ok=True)
    grub = tmp_path / "grub.cfg"
    grub.write_text(
        "set superusers=root\npassword_pbkdf2 root grub.pbkdf2...\n", encoding="utf-8"
    )
    flag = tmp_path / "synchronized"
    flag.write_text("", encoding="utf-8")
    return {
        "mail_configured": True,
        "last_backup": NOW - dt.timedelta(days=2),
        "now": NOW,
        "other_browsers": [tmp_path / "no-firefox"],
        "grub_configs": [grub],
        "timesync_flag": flag,
    }


def test_all_green_when_all_is_in_place(config: Config, tmp_path: Path) -> None:
    found = health.checks(config, GOOD_STATUS, **_everything_in_place(config, tmp_path))
    assert set(_levels(found).values()) == {Level.OK}
    assert health.needing_attention(found) == 0


def test_the_daemon_down_says_so_and_guesses_nothing(config: Config, tmp_path: Path) -> None:
    found = health.checks(config, None, **_everything_in_place(config, tmp_path))
    levels = _levels(found)
    assert levels["daemon"] is Level.BAD
    assert levels["filter"] is Level.INFO, "unknown, not green"
    assert found[0].key == "daemon", "what needs the parent first"


@pytest.mark.parametrize(
    "dns", [{"running": False, "open": False}, {"running": True, "open": True}]
)
def test_a_filter_that_is_down_or_open_is_red(
    config: Config, tmp_path: Path, dns: dict[str, bool]
) -> None:
    status = {**GOOD_STATUS, "dns_filter": dns}
    found = health.checks(config, status, **_everything_in_place(config, tmp_path))
    assert _levels(found)["filter"] is Level.BAD


def test_what_the_family_should_know_about(config: Config, tmp_path: Path) -> None:
    given = _everything_in_place(config, tmp_path)
    firefox = tmp_path / "firefox-esr"
    firefox.write_text("", encoding="utf-8")
    given.update(
        mail_configured=False,
        last_backup=None,
        other_browsers=[firefox],
        grub_configs=[tmp_path / "missing.cfg"],
        timesync_flag=tmp_path / "missing-flag",
    )
    (config.paths.chromium_policy_dir / POLICY_FILENAME).unlink()
    found = health.checks(config, GOOD_STATUS, **given)
    levels = _levels(found)
    assert levels["browser_policy"] is Level.BAD
    assert levels["other_browser"] is Level.WARN
    assert levels["mail"] is Level.WARN
    assert levels["backup"] is Level.INFO
    assert levels["boot_password"] is Level.INFO, "optional, never a warning"
    assert levels["clock"] is Level.INFO
    other = next(check for check in found if check.key == "other_browser")
    assert other.values == {"names": "firefox-esr"} and other.link == "/guides/machine"
    assert health.needing_attention(found) == 3


def test_an_old_backup_is_a_note(config: Config, tmp_path: Path) -> None:
    given = _everything_in_place(config, tmp_path)
    given["last_backup"] = NOW - dt.timedelta(days=45)
    found = health.checks(config, GOOD_STATUS, **given)
    backup = next(check for check in found if check.key == "backup")
    assert backup.level is Level.INFO and backup.values == {"days": 45}


def test_settings_show_the_list_and_today_the_line(
    client: TestClient, family_ready: None, daemon: FakeDaemon
) -> None:
    daemon.status = {"children": [], "dns_filter": {"running": False, "open": False}}
    login(client)
    settings = client.get("/settings").text
    assert "Koruma durumu" in settings
    assert "İnternet filtresi" in settings and "Ne yapmalıyım" in settings
    today = client.get("/today").text
    assert 'href="/settings#health"' in today


def test_the_machine_guide_is_there(client: TestClient) -> None:
    page = client.get("/guides/machine").text
    assert "Bilgisayarın kendisini korumak" in page
    assert "sudo apt remove firefox-esr" in page
