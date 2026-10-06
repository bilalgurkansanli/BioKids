"""One button for phone access: install Tailscale, sign in, publish the panel (D-169).

Until this, reaching the panel from a phone meant a parent at a terminal adding
Tailscale's apt repository, editing `/etc/default/tailscaled` so the client does
not upload its logs (D-157), and running `tailscale up`. The owner's verdict:
too hard for the parents BioKids is for. `biokids-remote-setup.service` runs
`biokids-admin remote-setup`, which is this module, when a parent presses
"Telefondan erişimi aç" and Tailscale is not ready.

What it does, and only when it has to:

1. Adds Tailscale's own apt repository, signed by the key BioKids ships (pinned
   below by fingerprint, never downloaded), and installs `tailscale`. Debian has
   no Tailscale package.
2. Writes `--no-logs-no-support` into `/etc/default/tailscaled` *before* the
   package is installed, so the client never uploads a log, not even on the
   first start (principle 1). An existing file keeps everything else.
3. Runs `tailscale up` and waits, up to half an hour, for the parent to sign in.
   Meanwhile `tailscale status --json` carries the sign-in page, and the panel
   shows it as a button and a QR code (`RemoteStatus.auth_url`).
4. Publishes the panel with `remote.enable()`, the same call the button makes
   when Tailscale is already up, so the Funnel refusal and the Serve approval
   page (D-157) work the same way.

There is still deliberately no way to put the panel on the local network or the
internet (`remote.py`).
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from biokids_core import remote
from biokids_core.remote import CommandRunner, RemoteResult, RemoteState

log = logging.getLogger(__name__)

KEYRING_SOURCE: Final = (
    Path(__file__).resolve().parent / "data" / "tailscale" / ("tailscale-archive-keyring.gpg")
)
KEY_FINGERPRINT: Final = "2596A99EAAB33821893C0A79458CA832957F5868"
"""Tailscale Inc.'s package repository signing key, as fetched from
pkgs.tailscale.com on 2026-10-06. A test checks the shipped file against it."""

KEYRING: Final = Path("/usr/share/keyrings/tailscale-archive-keyring.gpg")
SOURCES: Final = Path("/etc/apt/sources.list.d/tailscale.list")
DEFAULTS: Final = Path("/etc/default/tailscaled")
OS_RELEASE: Final = Path("/etc/os-release")
REPOSITORY: Final = "https://pkgs.tailscale.com/stable"
SUPPORTED: Final = frozenset({"debian", "ubuntu"})
"""The distributions Tailscale publishes packages for under these names."""

NO_LOGS: Final = "--no-logs-no-support"
DEFAULTS_TEXT: Final = f'PORT="41641"\nFLAGS="{NO_LOGS}"\n'
"""The package's own defaults file, with the one flag BioKids needs."""

LOGIN_WAIT: Final = "30m"
APT_TIMEOUT: Final = 900.0
LOGIN_TIMEOUT: Final = 31 * 60.0


@dataclass
class SetupResult:
    installed: bool = False
    """Tailscale was installed by this run."""
    logged_in: bool = False
    published: RemoteResult | None = None
    error: str = ""
    """Untranslated, for the audit log and the journal."""
    steps: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.error and self.published is not None and self.published.ok


def os_release(text: str) -> dict[str, str]:
    """`/etc/os-release` as a dict, quotes removed."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() and not key.startswith("#"):
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def sources_line(distribution: str, codename: str) -> str:
    return f"deb [signed-by={KEYRING.as_posix()}] {REPOSITORY}/{distribution} {codename} main\n"


def with_no_logs(text: str) -> str:
    """The defaults file with `--no-logs-no-support` among its FLAGS."""
    if not text.strip():
        return DEFAULTS_TEXT
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("FLAGS="):
            if NO_LOGS in line:
                return text
            flags = line.removeprefix("FLAGS=").strip().strip('"')
            joined = f"{flags} {NO_LOGS}".strip()
            lines[index] = f'FLAGS="{joined}"'
            return "\n".join(lines) + "\n"
    return text.rstrip("\n") + f'\nFLAGS="{NO_LOGS}"\n'


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".biokids")
    partial.write_text(text, encoding="utf-8")
    partial.chmod(0o644)
    partial.replace(path)


def setup(
    port: int,
    *,
    runner: CommandRunner | None = None,
    installed: Callable[[], bool] | None = None,
    release: str | None = None,
) -> SetupResult:
    """Do whatever is still missing, in order, and stop at the first failure."""
    result = SetupResult()
    command: CommandRunner = runner or remote.run_command
    is_installed = installed or (lambda: shutil.which(remote.TAILSCALE_BINARY) is not None)
    present = is_installed()

    def run(argv: list[str], timeout: float) -> remote.CommandResult:
        result.steps.append(" ".join(argv[:4]))
        return command(argv, timeout)

    distribution = codename = ""
    if not present:
        values = os_release(
            release if release is not None else OS_RELEASE.read_text(encoding="utf-8")
        )
        distribution, codename = values.get("ID", ""), values.get("VERSION_CODENAME", "")
        if distribution not in SUPPORTED or not codename:
            result.error = f"Tailscale has no packages for {distribution or 'this system'}"
            return result

    defaults_before = DEFAULTS.read_text(encoding="utf-8") if DEFAULTS.exists() else ""
    defaults_after = with_no_logs(defaults_before)
    if defaults_after != defaults_before:
        # Before the package: its postinst starts tailscaled, and that first
        # start must already be the quiet one.
        _write(DEFAULTS, defaults_after)

    if not present:
        KEYRING.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(KEYRING_SOURCE, KEYRING)
        KEYRING.chmod(0o644)
        _write(SOURCES, sources_line(distribution, codename))
        update = run(
            [
                "apt-get",
                "update",
                f"-o=Dir::Etc::sourcelist={SOURCES.as_posix()}",
                "-o=Dir::Etc::sourceparts=-",
                "-o=APT::Get::List-Cleanup=0",
            ],
            APT_TIMEOUT,
        )
        if not update.ok:
            result.error = f"apt-get update: {update.stderr.strip()[-300:]}"
            return result
        install = run(
            [
                "apt-get",
                "install",
                "--yes",
                "--no-install-recommends",
                "-o=Dpkg::Options::=--force-confold",
                "tailscale",
            ],
            APT_TIMEOUT,
        )
        if not install.ok:
            result.error = f"apt-get install: {install.stderr.strip()[-300:]}"
            return result
        result.installed = True

    run(["systemctl", "enable", "--now", "tailscaled.service"], 60.0)
    if defaults_after != defaults_before and not result.installed:
        # Already running, with logging on: restart it with the flag.
        run(["systemctl", "restart", "tailscaled.service"], 60.0)

    status = remote.TailscaleRemote(runner=command).detect()
    if status.state in {RemoteState.LOGGED_OUT, RemoteState.STOPPED}:
        up = run([remote.TAILSCALE_BINARY, "up", f"--timeout={LOGIN_WAIT}"], LOGIN_TIMEOUT)
        if not up.ok:
            result.error = f"tailscale up: {(up.stderr or up.stdout).strip()[-300:]}"
            return result
    result.logged_in = True

    result.published = remote.TailscaleRemote(runner=command).enable(port)
    if not result.published.ok and not result.published.approval_url:
        result.error = result.published.detail or result.published.error
    return result
