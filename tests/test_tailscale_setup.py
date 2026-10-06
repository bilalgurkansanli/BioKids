"""D-169: phone access in one button.

The setup may add a third party's apt repository to a family's machine, so what
it trusts is pinned and checked here: the key, the repository line, and the
order in which the client is told never to upload its logs.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar

import pytest

from biokids_core import remote, tailscale_setup
from biokids_core.remote import CommandResult, RemoteResult, RemoteState, RemoteStatus

TRIXIE = 'PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nID=debian\nVERSION_CODENAME=trixie\n'


def v4_fingerprint(keyring: bytes) -> str:
    """The OpenPGP v4 fingerprint of the first public key in a binary keyring."""
    tag = keyring[0]
    assert tag & 0x80, "not an OpenPGP packet"
    if tag & 0x40:  # new format
        assert tag & 0x3F == 6, "the first packet is not a public key"
        first = keyring[1]
        if first < 192:
            length, start = first, 2
        else:
            length, start = ((first - 192) << 8) + keyring[2] + 192, 3
    else:  # old format
        assert (tag >> 2) & 0x0F == 6, "the first packet is not a public key"
        size = {0: 1, 1: 2, 2: 4}[tag & 0x03]
        length = int.from_bytes(keyring[1 : 1 + size], "big")
        start = 1 + size
    body = keyring[start : start + length]
    assert body[0] == 4, "not a v4 key"
    return hashlib.sha1(b"\x99" + len(body).to_bytes(2, "big") + body).hexdigest().upper()


def test_the_shipped_key_is_tailscales_repository_key() -> None:
    keyring = tailscale_setup.KEYRING_SOURCE.read_bytes()
    assert v4_fingerprint(keyring) == tailscale_setup.KEY_FINGERPRINT


def test_the_repository_line_is_signed_by_that_key_only() -> None:
    line = tailscale_setup.sources_line("debian", "trixie")
    assert line == (
        "deb [signed-by=/usr/share/keyrings/tailscale-archive-keyring.gpg] "
        "https://pkgs.tailscale.com/stable/debian trixie main\n"
    )


def test_os_release_is_read_with_its_quotes_removed() -> None:
    values = tailscale_setup.os_release(TRIXIE)
    assert values["ID"] == "debian"
    assert values["VERSION_CODENAME"] == "trixie"


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("", 'PORT="41641"\nFLAGS="--no-logs-no-support"\n'),
        ('PORT="41641"\nFLAGS=""\n', 'PORT="41641"\nFLAGS="--no-logs-no-support"\n'),
        ('FLAGS="--verbose=1"\n', 'FLAGS="--verbose=1 --no-logs-no-support"\n'),
        ('PORT="41641"\n', 'PORT="41641"\nFLAGS="--no-logs-no-support"\n'),
    ],
)
def test_the_client_is_told_not_to_upload_its_logs(before: str, after: str) -> None:
    assert tailscale_setup.with_no_logs(before) == after


def test_a_defaults_file_that_already_says_so_is_left_alone() -> None:
    text = '# mine\nPORT="1"\nFLAGS="--no-logs-no-support --verbose=2"\n'
    assert tailscale_setup.with_no_logs(text) == text


class FakeRemote:
    """TailscaleRemote's two calls, scripted."""

    states: ClassVar[list[RemoteState]] = []
    enabled: ClassVar[list[int]] = []

    def __init__(self, runner: object = None) -> None:
        _ = runner

    def detect(self) -> RemoteStatus:
        return RemoteStatus(FakeRemote.states.pop(0))

    def enable(self, port: int | None = None) -> RemoteResult:
        FakeRemote.enabled.append(port or 0)
        return RemoteResult(ok=True)


@pytest.fixture
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("KEYRING", "SOURCES", "DEFAULTS"):
        target = tmp_path / getattr(tailscale_setup, name).name
        monkeypatch.setattr(tailscale_setup, name, target)
    monkeypatch.setattr(remote, "TailscaleRemote", FakeRemote)
    FakeRemote.states = []
    FakeRemote.enabled = []
    return tmp_path


def test_from_nothing_to_a_published_panel(machine: Path) -> None:
    ran: list[tuple[str, ...]] = []
    flags_at_install: list[str] = []

    def runner(argv: Sequence[str], timeout: float, /) -> CommandResult:
        ran.append(tuple(argv))
        if tuple(argv[:2]) == ("apt-get", "install"):
            flags_at_install.append(tailscale_setup.DEFAULTS.read_text(encoding="utf-8"))
        return CommandResult()

    FakeRemote.states = [RemoteState.LOGGED_OUT]
    result = tailscale_setup.setup(7878, runner=runner, installed=lambda: False, release=TRIXIE)

    assert result.ok, result.error
    assert result.installed and result.logged_in
    assert FakeRemote.enabled == [7878]
    assert "--no-logs-no-support" in flags_at_install[0], "before the first start"
    assert tailscale_setup.KEYRING.read_bytes() == tailscale_setup.KEYRING_SOURCE.read_bytes()
    assert "pkgs.tailscale.com/stable/debian trixie" in tailscale_setup.SOURCES.read_text()
    commands = [" ".join(call[:2]) for call in ran]
    assert commands.index("apt-get update") < commands.index("apt-get install")
    assert ("tailscale", "up", "--timeout=30m") in ran


def test_an_installed_and_signed_in_tailscale_is_only_published(machine: Path) -> None:
    ran: list[tuple[str, ...]] = []

    def runner(argv: Sequence[str], timeout: float, /) -> CommandResult:
        ran.append(tuple(argv))
        return CommandResult()

    tailscale_setup.DEFAULTS.write_text('PORT="41641"\nFLAGS="--no-logs-no-support"\n')
    FakeRemote.states = [RemoteState.RUNNING]
    result = tailscale_setup.setup(7878, runner=runner, installed=lambda: True)

    assert result.ok
    assert not any(call[0] == "apt-get" for call in ran)
    assert not any(call[:2] == ("tailscale", "up") for call in ran)
    assert not any("restart" in call for call in ran)
    assert not tailscale_setup.SOURCES.exists()


def test_a_running_client_that_uploads_logs_is_restarted_without(machine: Path) -> None:
    ran: list[tuple[str, ...]] = []

    def runner(argv: Sequence[str], timeout: float, /) -> CommandResult:
        ran.append(tuple(argv))
        return CommandResult()

    tailscale_setup.DEFAULTS.write_text('PORT="41641"\nFLAGS=""\n')
    FakeRemote.states = [RemoteState.RUNNING]
    tailscale_setup.setup(7878, runner=runner, installed=lambda: True)

    assert ("systemctl", "restart", "tailscaled.service") in ran
    assert "--no-logs-no-support" in tailscale_setup.DEFAULTS.read_text()


def test_an_unsupported_system_is_told_so_and_nothing_is_written(machine: Path) -> None:
    def runner(argv: Sequence[str], timeout: float, /) -> CommandResult:
        raise AssertionError(f"ran {argv}")

    result = tailscale_setup.setup(
        7878, runner=runner, installed=lambda: False, release="ID=arch\n"
    )
    assert not result.ok
    assert "arch" in result.error
    assert not tailscale_setup.DEFAULTS.exists()
    assert not tailscale_setup.SOURCES.exists()


def test_a_parent_who_never_signs_in_is_a_failure_not_a_hang(machine: Path) -> None:
    def runner(argv: Sequence[str], timeout: float, /) -> CommandResult:
        if tuple(argv[:2]) == ("tailscale", "up"):
            return CommandResult(returncode=1, stderr="timeout waiting for login")
        return CommandResult()

    FakeRemote.states = [RemoteState.LOGGED_OUT]
    result = tailscale_setup.setup(7878, runner=runner, installed=lambda: True)
    assert not result.ok
    assert "tailscale up" in result.error
    assert FakeRemote.enabled == []


# ------------------------------------------------------ what the panel is told


def test_the_sign_in_page_is_read_from_the_status_and_nothing_else_is() -> None:
    def status(auth_url: str) -> RemoteStatus:
        payload = json.dumps({"BackendState": "NeedsLogin", "AuthURL": auth_url})
        return remote.TailscaleRemote(
            runner=lambda _a, _t: CommandResult(stdout=payload)
        ).detect()

    good = "https://login.tailscale.com/a/9edb3e9016be4"
    assert status(good).auth_url == good
    assert status("https://evil.example/a/123").auth_url == ""
    assert status(good + "?next=https://evil.example").auth_url == ""


@pytest.mark.parametrize(
    ("word", "state"),
    [("activating", "running"), ("failed", "failed"), ("inactive", ""), ("", "")],
)
def test_the_setup_units_state(word: str, state: str) -> None:
    def runner(argv: Sequence[str], timeout: float, /) -> CommandResult:
        assert tuple(argv) == ("systemctl", "is-active", remote.SETUP_UNIT)
        return CommandResult(returncode=0 if word == "activating" else 3, stdout=word + "\n")

    assert remote.setup_state(runner) == state
