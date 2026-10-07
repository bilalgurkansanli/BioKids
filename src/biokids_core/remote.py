"""Remote access to the parent panel: Tailscale, or nothing at all.

The panel binds `127.0.0.1:7878` and never anything else (D-33). When a parent
wants it on their phone, `tailscale serve --bg 7878` publishes it inside their
own tailnet, over HTTPS, on a name only devices they have personally added to
that tailnet can resolve. No port is opened on the router, no reverse proxy is
configured, and nothing about the panel becomes visible from the internet.

**There is deliberately no "expose it on the local network" option, and there
never will be.** It looks harmless — a home LAN, one more checkbox next to the
Tailscale one — and it is not. Someone will tick it, find that it works, and
then forward the port on their router so it also works from the office. What is
behind that port is a live video of a child's day and a control surface for
their computer, protected by six digits. The only way to keep that from
happening is to never ship the checkbox. When Tailscale is absent the panel says
"localhost only" (`remote.localhost_only`) and tells the parent, in as many
words, not to forward a port (`remote.no_port_forward`). That is the whole
fallback. See `TASKS-PHASE0.md` T7.3.

Tailscale has its own version of that checkbox — **Funnel**, which puts a served
port on the public internet — and a parent may have switched it on for this node
long before BioKids arrived. So the serve config is read for `AllowFunnel`, not
just for handlers: if the tailnet port we publish on is open to the world we
refuse to publish the panel on it, and `RemoteStatus.public` tells the panel to
stop promising that only the family's devices can reach the address. We refuse;
we do not quietly rearrange a parent's own tailnet settings for them.

**We never terminate TLS ourselves** (D-52). Tailscale issues and terminates the
certificate for `<host>.<tailnet>.ts.net`; BioKids holds no private key, runs no
ACME client, and has no certificate to let expire on a family's machine two
years after they stopped thinking about it.

`detect()`, `enable()` and `disable()` never raise. `detect()` is called while
the Today screen renders, and a tailscaled that is wedged, missing, or answering
with something we cannot parse must cost the parent a status line, never the
page. `qr_svg()` is the exception: it is one wizard step, it needs an encoder,
and a missing encoder is a packaging bug that should be loud.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Protocol

from biokids_core.config import Config, get_config

TAILSCALE_BINARY: Final = "tailscale"

SERVE_HTTPS_PORT: Final = 443
"""The tailnet-facing port. `tailscale serve` uses 443 unless told otherwise, and
telling it otherwise would mean printing a port in the URL a parent has to type."""

DETECT_TIMEOUT: Final = 4.0
"""Long enough for a busy tailscaled, short enough that the panel still paints."""

ACTION_TIMEOUT: Final = 15.0
"""Turning serve on talks to the coordination server the first time, so it gets
more room than a status query — but still a bound, because a parent pressing a
button deserves an answer either way."""

_EXIT_FAILED: Final = -1
_EXIT_NOT_FOUND: Final = 127


class RemoteState(StrEnum):
    """Everything the panel needs to know, in one word.

    Five states, because five is what a parent can be told apart: it is not
    installed, it is installed but not signed in, it is signed in but not up, it
    is up but the panel is not published, the panel is published.
    """

    ABSENT = "absent"
    LOGGED_OUT = "logged_out"
    STOPPED = "stopped"
    RUNNING = "running"
    SERVING = "serving"

    @property
    def i18n_key(self) -> str:
        """The catalog key that names this state to a parent, in their language."""
        return f"remote.status.{self.value}"

    @property
    def ready(self) -> bool:
        """True when tailscaled is up, so turning serve on or off can be attempted."""
        return self in {RemoteState.RUNNING, RemoteState.SERVING}


class RemoteCopy:
    """Keys the remote-access screens need, named here rather than in a template.

    T7.3 asks for specific copy when Tailscale is absent. Putting the keys next
    to the code that decides the state keeps the two from drifting.
    """

    ENABLE: Final = "remote.enable"
    DISABLE: Final = "remote.disable"
    URL_LABEL: Final = "remote.url"
    QR_HELP: Final = "remote.qr.help"
    INSTALL_HELP: Final = "remote.install.help"
    LOCALHOST_ONLY: Final = "remote.localhost_only"
    NO_PORT_FORWARD: Final = "remote.no_port_forward"
    ONLY_TAILNET: Final = "remote.only_tailnet"


class RemoteError:
    """i18n keys this module may hand back. Every one exists in both catalogs.

    The states double as error messages: refusing to enable remote access
    because Tailscale is stopped is best explained by `remote.status.stopped`,
    which already says "Tailscale is stopped" in both languages. Only the
    failures that are not a state need a key of their own.
    """

    COMMAND_FAILED: Final = "error.save_failed"
    BAD_PORT: Final = "error.invalid_input"

    WOULD_BE_PUBLIC: Final = RemoteCopy.NO_PORT_FORWARD
    """Refusing to publish through an open Funnel. "Do not put the panel on the
    internet" is the reason, and it is already the sentence we show a parent."""

    NEEDS_APPROVAL: Final = "remote.needs_approval"
    """Serve is off for this tailnet, and Tailscale printed the page that turns it
    on. `RemoteResult.approval_url` carries the page."""


AUTH_URL_RE: Final = re.compile(r"^https://login\.tailscale\.com/a/[A-Za-z0-9]+$")
"""What `tailscale up` asks a person to open. Anything else is not shown."""

APPROVAL_URL_RE: Final = re.compile(
    r"https://login\.tailscale\.com/f/[a-z]+\?node=[A-Za-z0-9]+"
)
"""The only link this module will ever hand a parent to click. Anything that does
not match exactly - another host, another path, anything after the node id - is
not shown, so the panel cannot be made to present somebody else's link as a
step in setting it up."""


SETTING_APPROVAL_URL: Final = "remote.approval_url"
"""Where the panel keeps the last approval page Tailscale asked for, between the
button press that found it and the page that shows it. Stored rather than put
in the redirect's query string, which anyone could fill with a link of their
own; and read back through `approval_url_in`, never trusted as it stands."""


def approval_url_in(text: str) -> str:
    """The Tailscale approval page named in `text`, or ""."""
    found = APPROVAL_URL_RE.search(text or "")
    return found.group(0) if found else ""


# ------------------------------------------------------------------ the shell out


@dataclass(frozen=True, slots=True)
class CommandResult:
    """What a command did.

    A missing binary and an expired timeout are fields rather than exceptions
    because every caller in this module has to treat them as answers, not as
    accidents.
    """

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    missing: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.missing


class CommandRunner(Protocol):
    """How this module reaches the outside world.

    Injected, so the tests can describe a whole tailnet without monkeypatching
    `subprocess` for the rest of the suite.
    """

    def __call__(self, argv: Sequence[str], timeout: float, /) -> CommandResult: ...


def run_command(argv: Sequence[str], timeout: float, /) -> CommandResult:
    """The real runner: `subprocess.run`, with every failure turned into a result.

    argv is always a list and never a shell string, and the only value this
    module ever interpolates into it is a port it has already checked is an
    integer in range.
    """
    try:
        completed = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return CommandResult(returncode=_EXIT_NOT_FOUND, missing=True)
    except subprocess.TimeoutExpired as exc:
        # Kept, not dropped: `tailscale serve` on a tailnet where Serve is off
        # prints the page that turns it on and then waits for someone to visit
        # it. What it printed is the only way to tell the parent where to go.
        return CommandResult(
            returncode=_EXIT_FAILED,
            stdout=_output_text(exc.stdout),
            stderr=_output_text(exc.stderr),
            timed_out=True,
        )
    except OSError as exc:
        # Exists but will not exec: a broken install, or no permission to run it.
        return CommandResult(returncode=_EXIT_FAILED, stderr=str(exc))
    return CommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


# --------------------------------------------------------------------- status shape


@dataclass(frozen=True, slots=True)
class RemoteStatus:
    """One reading of the machine's Tailscale situation."""

    state: RemoteState
    url: str | None = None
    """The panel's tailnet address, set only when there is one that works."""
    hostname: str = ""
    tailnet: str = ""
    served_ports: tuple[int, ...] = ()
    """Local ports `tailscale serve` is currently publishing, whoever set them."""
    funnel_ports: tuple[int, ...] = ()
    """Tailnet ports this node has opened to the public internet with Funnel."""
    detail: str = ""
    """A short technical note for the audit log. Untranslated, never shown."""
    auth_url: str = ""
    """While signed out after `tailscale up`: the page that signs this machine in
    to the parent's tailnet (D-169). Only ever a login.tailscale.com address."""
    phones: tuple[str, ...] = ()
    """Names of the phones and tablets in the same tailnet, so the guide can say
    that step three is done (D-195)."""

    @property
    def status_key(self) -> str:
        return self.state.i18n_key

    @property
    def ready(self) -> bool:
        """Forwarded from the state so a template can ask the status directly.

        Jinja resolves a missing attribute to Undefined and renders the branch
        as false, so `status.ready` has to exist here rather than only on the
        enum: a settings page that quietly stops marking Tailscale as up is the
        kind of bug nobody files.
        """
        return self.state.ready

    def serves(self, port: int) -> bool:
        return port in self.served_ports

    @property
    def public(self) -> bool:
        """True when what we publish on would be reachable from the internet.

        The panel must not show `remote.only_tailnet` next to an address that a
        Funnel has already made world-readable — that sentence is the promise
        the whole feature rests on, and it has to be checked rather than
        assumed.
        """
        return SERVE_HTTPS_PORT in self.funnel_ports


@dataclass(frozen=True, slots=True)
class RemoteResult:
    """The answer to "turn it on" or "turn it off"."""

    ok: bool
    error: str = ""
    """An i18n key, never a sentence. The panel translates it."""
    status: RemoteStatus | None = None
    """The state after the attempt, so the caller can re-render without asking again."""
    detail: str = ""
    approval_url: str = ""
    """Set with `RemoteError.NEEDS_APPROVAL`: the page the parent must open once."""


# ------------------------------------------------------------------------ the client


class TailscaleRemote:
    """Reads and changes the `tailscale serve` mapping for the panel port.

    The panel holds one of these for the life of the process. Nothing about the
    tailnet is cached on it: every question is asked again, because "remote
    access is on" is exactly the kind of answer that goes stale between the
    moment it is cached and the moment a parent believes it.
    """

    def __init__(
        self,
        config: Config | None = None,
        runner: CommandRunner | None = None,
        *,
        binary: str = TAILSCALE_BINARY,
    ) -> None:
        self._config = config or get_config()
        self._runner: CommandRunner = runner or run_command
        self.binary = binary

    @property
    def port(self) -> int:
        """The panel port from config — 7878 unless an admin moved it."""
        return self._config.panel.port

    # ------------------------------------------------------------ detection

    def detect(self) -> RemoteStatus:
        """Read the current state. Never raises, never blocks for long.

        `tailscale status --json` exits non-zero when the node is logged out but
        still prints usable JSON, so the exit code is ignored and stdout is
        parsed either way. When the binary is there but nothing readable comes
        back — a timeout, a half-written line, a wedged daemon — the answer is
        STOPPED rather than RUNNING: claiming remote access works when we cannot
        confirm it is exactly the silent failure principle 3 forbids.
        """
        probe = self._run((self.binary, "status", "--json"), DETECT_TIMEOUT)
        if probe.missing:
            return RemoteStatus(RemoteState.ABSENT, detail="tailscale is not on PATH")

        payload = _load_json(probe.stdout)
        if payload is None:
            return RemoteStatus(
                RemoteState.STOPPED,
                detail=_why_unreadable(probe) or "no JSON in status output",
            )

        dns_name = _dns_name(payload)
        hostname, tailnet = _split_name(dns_name)
        state = _BACKEND_STATES.get(_text(payload.get("BackendState")), RemoteState.STOPPED)
        if state is not RemoteState.RUNNING:
            # A URL is only printed for a node that can answer on it, so a
            # stopped machine never shows a parent an address that times out.
            auth_url = _text(payload.get("AuthURL"))
            return RemoteStatus(
                state,
                hostname=hostname,
                tailnet=tailnet,
                auth_url=auth_url if AUTH_URL_RE.match(auth_url) else "",
            )

        serve = self._serve_config()
        url = _panel_url(dns_name)
        if url is None:
            # Up, but with no MagicDNS name we recognise: there is nothing to
            # put on a phone, so this is as good as not published.
            return RemoteStatus(
                RemoteState.RUNNING,
                hostname=hostname,
                tailnet=tailnet,
                served_ports=serve.ports,
                funnel_ports=serve.funnel_ports,
                detail="no usable MagicDNS name in status output",
                phones=_phones(payload),
            )
        serving = self.port in serve.ports
        return RemoteStatus(
            RemoteState.SERVING if serving else RemoteState.RUNNING,
            url=url,
            hostname=hostname,
            tailnet=tailnet,
            served_ports=serve.ports,
            funnel_ports=serve.funnel_ports,
            phones=_phones(payload),
        )

    def panel_url(self) -> str | None:
        """`https://<host>.<tailnet>.ts.net`, or None when there is no such address."""
        return self.detect().url

    # --------------------------------------------------------------- actions

    def enable(self, port: int | None = None) -> RemoteResult:
        """Publish the panel port to the tailnet with `tailscale serve --bg`.

        Success is judged by asking Tailscale afterwards, not by the exit code:
        the only claim worth making to a parent is that the panel is reachable,
        and that is a question only `serve status` can answer.

        A node with Funnel open on the tailnet port is refused before anything
        runs. `serve` would happily attach the panel to it, and the result would
        not be "your family's devices can reach this" but "anyone can".
        """
        target = self.port if port is None else port
        if not _valid_port(target):
            return RemoteResult(ok=False, error=RemoteError.BAD_PORT, detail=f"port {target}")

        before = self.detect()
        if not before.state.ready:
            return RemoteResult(ok=False, error=before.status_key, status=before)
        if before.public:
            return RemoteResult(
                ok=False,
                error=RemoteError.WOULD_BE_PUBLIC,
                status=before,
                detail=f"tailscale funnel is open on port {SERVE_HTTPS_PORT}",
            )

        outcome = self._run((self.binary, "serve", "--bg", str(target)), ACTION_TIMEOUT)
        after = self.detect()
        if after.serves(target) and after.url is not None and not after.public:
            return RemoteResult(ok=True, status=after)
        approval = approval_url_in(outcome.stdout) or approval_url_in(outcome.stderr)
        if approval:
            # Every new tailnet starts here: Serve is off until its owner turns
            # it on, and `serve` waits for them without saying so to anyone but
            # a terminal. Found on the VM, where the panel said only "could not
            # be changed" (D-157).
            return RemoteResult(
                ok=False,
                error=RemoteError.NEEDS_APPROVAL,
                status=after,
                detail=f"serve is not enabled on this tailnet; approve at {approval}",
                approval_url=approval,
            )
        return RemoteResult(
            ok=False,
            error=RemoteError.WOULD_BE_PUBLIC if after.public else RemoteError.COMMAND_FAILED,
            status=after,
            detail=_enable_detail(outcome, after, target),
        )

    def disable(self, port: int | None = None) -> RemoteResult:
        """Stop publishing the panel, without resetting the rest of serve.

        The command drops the handlers on the tailnet HTTPS port rather than
        running `tailscale serve reset`, so TCP forwards and anything published
        on another port survive. Handlers a parent put on other paths of 443 do
        not; that is the cost of not owning the whole config, and it is the
        smaller cost.

        A node that is not up publishes nothing right now, but the stored serve
        config outlives it, so "off" is only reported when the command that
        removes it actually succeeded. Turning remote access off and finding it
        back on after a reboot is the failure this guards against.
        """
        target = self.port if port is None else port
        if not _valid_port(target):
            return RemoteResult(ok=False, error=RemoteError.BAD_PORT, detail=f"port {target}")

        before = self.detect()
        if before.state is RemoteState.ABSENT:
            return RemoteResult(ok=False, error=before.status_key, status=before)

        outcome = self._run(
            (self.binary, "serve", f"--https={SERVE_HTTPS_PORT}", "off"), ACTION_TIMEOUT
        )
        after = self.detect()
        # Two things have to hold before "off" is an honest answer: the node no
        # longer reports our handler, and — when it is too far down to report
        # one either way — the command that edits the stored config succeeded.
        if not after.serves(target) and (before.state.ready or outcome.ok):
            return RemoteResult(ok=True, status=after)
        return RemoteResult(
            ok=False,
            error=RemoteError.COMMAND_FAILED,
            status=after,
            detail=_why_unreadable(outcome)
            or outcome.stderr.strip()
            or "the node is down and the serve config was not changed",
        )

    # --------------------------------------------------------------- private

    def _serve_config(self) -> _ServeConfig:
        probe = self._run((self.binary, "serve", "status", "--json"), DETECT_TIMEOUT)
        return _parse_serve_config(_load_json(probe.stdout))

    def _run(self, argv: Sequence[str], timeout: float) -> CommandResult:
        """Call the runner and swallow anything it throws.

        The broad catch is the point: this is the single seam between a
        parent-facing page and an external program, and the contract for
        everything above it is that it returns a state. A runner that raises —
        a platform without fork, a stub in someone else's test, a future
        subprocess error we have not met — becomes a failed command here.
        """
        try:
            return self._runner(tuple(argv), timeout)
        except Exception as exc:
            return CommandResult(returncode=_EXIT_FAILED, stderr=f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------- default instance


SETUP_UNIT: Final = "biokids-remote-setup.service"
"""Installs Tailscale and waits for the parent to sign in (D-169)."""


def setup_state(runner: CommandRunner | None = None) -> str:
    """ "running" while that unit works, "failed" after it failed, otherwise "".

    A oneshot unit is "activating" for as long as it runs and "inactive" once it
    has succeeded; systemd keeps "failed" until the next start, which is what
    lets the screen say so and offer the button again.
    """
    word = (runner or run_command)(("systemctl", "is-active", SETUP_UNIT), DETECT_TIMEOUT)
    state = word.stdout.strip()
    if state in {"activating", "active", "reloading", "deactivating"}:
        return "running"
    return "failed" if state == "failed" else ""


def detect(config: Config | None = None) -> RemoteStatus:
    """Read the state with the real runner. The wizard and the CLI use this."""
    return TailscaleRemote(config).detect()


def panel_url(config: Config | None = None) -> str | None:
    return TailscaleRemote(config).panel_url()


def enable(port: int | None = None, config: Config | None = None) -> RemoteResult:
    return TailscaleRemote(config).enable(port)


def disable(port: int | None = None, config: Config | None = None) -> RemoteResult:
    return TailscaleRemote(config).disable(port)


# --------------------------------------------------------------------- QR rendering

QR_BOX_SIZE: Final = 10
"""qrcode's unit: 10 means one module per millimetre, so the code prints at a size
a phone can read from a sheet of paper as well as from a screen."""

QR_BORDER: Final = 4
"""The quiet zone the QR specification requires. Scanners fail without it."""


def qr_svg(url: str) -> str:
    """Render `url` as an SVG QR code for the wizard step.

    Server-side SVG: no JavaScript, no image encoder, and above all no call to
    an online QR service, which would put the family's private panel address in
    somebody else's access log for the sake of a picture we can draw ourselves.

    The result is a fragment — one white rectangle and one black path — meant to
    be embedded in a figure the template labels with `remote.qr.help`. It carries
    no title of its own because the address is printed next to it as text, which
    is what a screen reader should read out.

    `qrcode` is imported here rather than at module scope on purpose:
    `biokids-core` depends on python3, SQLAlchemy and Alembic only, and the
    daemon reads `detect()` on a machine that has no reason to own a QR encoder.
    The dependency belongs to `biokids-panel` (`PACKAGING.md`).
    """
    target = url.strip()
    if not target:
        raise ValueError("qr_svg needs a URL to encode")

    import qrcode
    from qrcode.image.svg import SvgPathFillImage

    code = qrcode.QRCode(
        # Medium correction: a phone camera reading a screen at an angle, or a
        # printout with a thumbprint on it, still resolves.
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=QR_BOX_SIZE,
        border=QR_BORDER,
        image_factory=SvgPathFillImage,
    )
    code.add_data(target)
    code.make(fit=True)

    # to_string() returns us-ascii bytes on every version shipped so far; the
    # isinstance guard keeps a future str return from becoming "b'<svg...'".
    payload: bytes | str = code.make_image().to_string()
    return payload.decode("ascii") if isinstance(payload, bytes) else payload


# ------------------------------------------------------------------------- parsing

_BACKEND_STATES: Final[Mapping[str, RemoteState]] = {
    "Running": RemoteState.RUNNING,
    "Stopped": RemoteState.STOPPED,
    # Starting is not Running: the node has no working address yet, and telling
    # a parent it is up when the next tap times out is worse than one more wait.
    "Starting": RemoteState.STOPPED,
    "NoState": RemoteState.STOPPED,
    "NeedsLogin": RemoteState.LOGGED_OUT,
    "NeedsMachineAuth": RemoteState.LOGGED_OUT,
}

_MAGIC_DNS_SUFFIX: Final = ".ts.net"

_HOSTNAME_RE: Final = re.compile(
    r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$"
)

_PORT_SUFFIX_RE: Final = re.compile(r":(\d{1,5})(?:/.*)?$")


def _valid_port(port: int) -> bool:
    return 1 <= port <= 65535


def _load_json(text: str) -> Mapping[str, Any] | None:
    """Parse a JSON object, or give up quietly.

    Every reason this can fail — an empty stdout because the command timed out,
    a truncated line, an error message where JSON was expected, a version of the
    CLI that prints an array — has the same answer: we learned nothing.
    """
    raw = text.strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


PHONE_SYSTEMS: Final = frozenset({"ios", "android"})
"""`OS` values of a peer that is a phone or a tablet, lower-cased."""


def _phones(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """The phones and tablets in this tailnet, by the name Tailscale shows."""
    peers = payload.get("Peer")
    if not isinstance(peers, Mapping):
        return ()
    names = {
        _text(peer.get("HostName")) or _text(peer.get("DNSName")).split(".")[0]
        for peer in peers.values()
        if isinstance(peer, Mapping) and _text(peer.get("OS")).lower() in PHONE_SYSTEMS
    }
    return tuple(sorted(name for name in names if name))


def _dns_name(payload: Mapping[str, Any]) -> str:
    """The node's MagicDNS name, from whichever field this version provides."""
    node = payload.get("Self")
    hostname = ""
    if isinstance(node, Mapping):
        direct = _text(node.get("DNSName"))
        if direct:
            return direct
        hostname = _text(node.get("HostName"))

    suffix = _text(payload.get("MagicDNSSuffix"))
    if not suffix:
        tailnet = payload.get("CurrentTailnet")
        if isinstance(tailnet, Mapping):
            suffix = _text(tailnet.get("MagicDNSSuffix"))
    return f"{hostname}.{suffix}" if hostname and suffix else ""


def _split_name(dns_name: str) -> tuple[str, str]:
    host, _, tailnet = dns_name.strip().rstrip(".").partition(".")
    return host, tailnet


def _panel_url(dns_name: str) -> str | None:
    """Build the panel address, or None if the name is not one we recognise.

    Only MagicDNS names are accepted. That is the single shape Tailscale hands
    out, and refusing everything else means a surprising status payload can
    never turn into a link the panel invites a parent to open.
    """
    host = dns_name.strip().rstrip(".").lower()
    if not host.endswith(_MAGIC_DNS_SUFFIX) or not _HOSTNAME_RE.match(host):
        return None
    return f"https://{host}"


@dataclass(frozen=True, slots=True)
class _ServeConfig:
    """What one reading of `tailscale serve status --json` told us."""

    ports: tuple[int, ...] = ()
    funnel_ports: tuple[int, ...] = ()


def _parse_serve_config(payload: Mapping[str, Any] | None) -> _ServeConfig:
    """Read the served local ports and the funnelled tailnet ports.

    The config is `{"Web": {"host:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:7878"}}}}}`
    with `{"AllowFunnel": {"host:443": true}}` beside it, and every level of it
    is checked rather than assumed: this is the one place where a shape change
    in a tool we do not ship could otherwise crash the panel's home screen.

    `Foreground` holds the same structure again for each `tailscale serve` still
    running in somebody's terminal. Those handlers publish exactly as much as
    the backgrounded ones do, so they count here too — a foreground funnel is
    still the child's panel on the open internet.
    """
    served: set[int] = set()
    funnelled: set[int] = set()
    for config in _serve_configs(payload):
        served |= _web_ports(config)
        funnelled |= _funnel_ports(config)
    return _ServeConfig(tuple(sorted(served)), tuple(sorted(funnelled)))


def _serve_configs(payload: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    """The config itself plus each foreground session's nested copy of it.

    One level down is enough: tailscaled does not nest a session inside a
    session, and a bounded walk cannot be talked into recursing forever by a
    payload we did not write.
    """
    if payload is None:
        return []
    configs = [payload]
    foreground = payload.get("Foreground")
    if isinstance(foreground, Mapping):
        configs += [session for session in foreground.values() if isinstance(session, Mapping)]
    return configs


def _web_ports(config: Mapping[str, Any]) -> set[int]:
    web = config.get("Web")
    if not isinstance(web, Mapping):
        return set()

    found: set[int] = set()
    for entry in web.values():
        if not isinstance(entry, Mapping):
            continue
        handlers = entry.get("Handlers")
        if not isinstance(handlers, Mapping):
            continue
        for handler in handlers.values():
            if not isinstance(handler, Mapping):
                continue
            port = _proxy_port(_text(handler.get("Proxy")))
            if port is not None:
                found.add(port)
    return found


def _funnel_ports(config: Mapping[str, Any]) -> set[int]:
    """Tailnet ports with Funnel switched on, keyed `"host:443"` -> true.

    Only an explicit `true` counts. Anything else — a missing map, a string, a
    version that renames the field — leaves the port off the list, and the panel
    keeps asking `enable()` before it publishes anything, which asks again.
    """
    allow = config.get("AllowFunnel")
    if not isinstance(allow, Mapping):
        return set()

    found: set[int] = set()
    for hostport, allowed in allow.items():
        if allowed is not True or not isinstance(hostport, str):
            continue
        # "host:443" is the shape tailscaled writes, but a bare port is read as
        # well: the expensive mistake here is failing to notice a funnel.
        # isdecimal rather than isdigit, because int("²") raises and nothing in
        # this module may raise over a payload we did not write.
        port = int(hostport) if hostport.isdecimal() else _proxy_port(hostport)
        if port is not None and _valid_port(port):
            found.add(port)
    return found


def _proxy_port(target: str) -> int | None:
    match = _PORT_SUFFIX_RE.search(target)
    if match is None:
        return None
    port = int(match.group(1))
    return port if _valid_port(port) else None


def _enable_detail(outcome: CommandResult, after: RemoteStatus, port: int) -> str:
    """Why the audit log should say `enable` failed.

    The command exiting zero and the panel being reachable are different
    claims, and the gap between them is worth naming: a mapping that took but
    has no address to reach it by looks, in a log full of `error.save_failed`,
    exactly like a command that did nothing.
    """
    if after.public:
        return f"tailscale funnel is open on port {SERVE_HTTPS_PORT}"
    if after.serves(port) and after.url is None:
        return "published, but the node has no MagicDNS name to reach it by"
    return _why_unreadable(outcome) or outcome.stderr.strip()


def _output_text(value: str | bytes | None) -> str:
    """`TimeoutExpired` output is bytes whatever `text=True` said, or None."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def _why_unreadable(result: CommandResult) -> str:
    if result.timed_out:
        return "tailscale did not answer in time"
    if result.missing:
        return "tailscale is not on PATH"
    if not result.ok:
        return f"tailscale exited {result.returncode}"
    return ""
