"""Tests for the Tailscale integration (T7).

The subprocess layer is injected, never monkeypatched: `TailscaleRemote` takes a
runner, so a whole tailnet — logged out, stopped, serving the wrong port — is
described by a few dataclasses, and the rest of the suite keeps the real
`subprocess` it expects. `run_command` itself is exercised once, against a
binary that does not exist, because "the binary is missing" is the state a
parent is most likely to be in.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from xml.etree import ElementTree

import pytest

from biokids_core import remote as remote_module
from biokids_core.config import Config
from biokids_core.i18n import load_catalog
from biokids_core.remote import (
    CommandResult,
    RemoteCopy,
    RemoteError,
    RemoteState,
    TailscaleRemote,
    approval_url_in,
    qr_svg,
    run_command,
)

PANEL_PORT = 7878
HOST = "biokids-vm.tail1a2b3c.ts.net"
DNS_NAME = f"{HOST}."
PANEL_URL = f"https://{HOST}"


# ------------------------------------------------------------------- fake tailscale


def status_json(backend: str = "Running", *, dns_name: str = DNS_NAME) -> str:
    return json.dumps(
        {
            "Version": "1.70.0",
            "BackendState": backend,
            "MagicDNSSuffix": "tail1a2b3c.ts.net",
            "CurrentTailnet": {"MagicDNSSuffix": "tail1a2b3c.ts.net"},
            "Self": {"HostName": "biokids-vm", "DNSName": dns_name, "Online": True},
        }
    )


def serve_json(port: int | None, *, funnel_port: int | None = None) -> str:
    """What `tailscale serve status --json` prints. `{}` when nothing is served."""
    config: dict[str, object] = {}
    if port is not None:
        config["TCP"] = {"443": {"HTTPS": True}}
        config["Web"] = {
            f"{HOST}:443": {"Handlers": {"/": {"Proxy": f"http://127.0.0.1:{port}"}}}
        }
    if funnel_port is not None:
        config["AllowFunnel"] = {f"{HOST}:{funnel_port}": True}
    return json.dumps(config)


class FakeTailscale:
    """A tailscaled that answers from a script, and remembers what it was asked.

    `serve --bg` and `serve off` mutate the fake's own serve config, so the
    "did it actually take effect?" check in `enable` and `disable` is exercised
    the way it will be in production instead of being stubbed past.
    """

    def __init__(
        self,
        *,
        backend: str = "Running",
        served_port: int | None = None,
        funnel_port: int | None = None,
        status: CommandResult | None = None,
        serve_status: CommandResult | None = None,
        action: CommandResult | None = None,
        obeys_actions: bool = True,
    ) -> None:
        self.backend = backend
        self.served_port = served_port
        self.funnel_port = funnel_port
        self.status = status
        self.serve_status = serve_status
        self.action = action
        self.obeys_actions = obeys_actions
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: Sequence[str], timeout: float, /) -> CommandResult:
        parts = tuple(argv)
        self.calls.append(parts)
        assert timeout > 0

        if parts[1:] == ("status", "--json"):
            return self.status or CommandResult(stdout=status_json(self.backend))
        if parts[1:] == ("serve", "status", "--json"):
            return self.serve_status or CommandResult(
                stdout=serve_json(self.served_port, funnel_port=self.funnel_port)
            )
        if parts[1:3] == ("serve", "--bg"):
            if self.obeys_actions:
                self.served_port = int(parts[3])
            return self.action or CommandResult()
        if parts[1] == "serve" and parts[-1] == "off":
            if self.obeys_actions:
                self.served_port = None
            return self.action or CommandResult()
        raise AssertionError(f"unexpected command: {parts}")

    def commands(self) -> list[str]:
        return [" ".join(call) for call in self.calls]


def remote(fake: FakeTailscale) -> TailscaleRemote:
    return TailscaleRemote(Config(), fake)


# --------------------------------------------------------------------- every state


def test_absent_when_the_binary_is_not_installed() -> None:
    fake = FakeTailscale(status=CommandResult(returncode=127, missing=True))
    status = remote(fake).detect()

    assert status.state is RemoteState.ABSENT
    assert status.url is None
    assert status.status_key == "remote.status.absent"
    # No point asking about serve config when there is nothing to ask.
    assert fake.commands() == ["tailscale status --json"]


def test_logged_out_when_the_backend_needs_a_login() -> None:
    status = remote(FakeTailscale(backend="NeedsLogin")).detect()

    assert status.state is RemoteState.LOGGED_OUT
    assert status.status_key == "remote.status.logged_out"
    assert status.url is None
    # The name is still reported: the wizard explains what the tailnet is.
    assert status.hostname == "biokids-vm"
    assert status.tailnet == "tail1a2b3c.ts.net"


def test_logged_out_survives_the_non_zero_exit_that_comes_with_it() -> None:
    """`tailscale status --json` exits 1 when logged out but still prints JSON."""
    fake = FakeTailscale(
        status=CommandResult(
            returncode=1, stdout=status_json("NeedsLogin"), stderr="Logged out."
        )
    )
    assert remote(fake).detect().state is RemoteState.LOGGED_OUT


def test_stopped_when_the_backend_is_down() -> None:
    status = remote(FakeTailscale(backend="Stopped")).detect()

    assert status.state is RemoteState.STOPPED
    assert status.status_key == "remote.status.stopped"
    assert status.url is None


def test_starting_counts_as_stopped_because_the_address_does_not_work_yet() -> None:
    assert remote(FakeTailscale(backend="Starting")).detect().state is RemoteState.STOPPED


def test_an_unknown_backend_state_is_stopped_not_running() -> None:
    assert remote(FakeTailscale(backend="Sideways")).detect().state is RemoteState.STOPPED


def test_running_when_up_but_nothing_is_published() -> None:
    status = remote(FakeTailscale(served_port=None)).detect()

    assert status.state is RemoteState.RUNNING
    assert status.status_key == "remote.status.running"
    assert status.served_ports == ()
    assert status.url == PANEL_URL


def test_running_when_serve_publishes_a_different_port() -> None:
    status = remote(FakeTailscale(served_port=9000)).detect()

    assert status.state is RemoteState.RUNNING
    assert status.served_ports == (9000,)
    assert not status.serves(PANEL_PORT)


def test_the_status_answers_ready_the_way_a_template_asks_it() -> None:
    """`settings.html` reads `tailscale.ready`, and Jinja hides a typo."""
    assert remote(FakeTailscale(served_port=PANEL_PORT)).detect().ready
    assert remote(FakeTailscale(backend="Running")).detect().ready
    assert not remote(FakeTailscale(backend="Stopped")).detect().ready
    assert not remote(FakeTailscale(backend="NeedsLogin")).detect().ready


def test_serving_when_the_panel_port_is_published() -> None:
    status = remote(FakeTailscale(served_port=PANEL_PORT)).detect()

    assert status.state is RemoteState.SERVING
    assert status.status_key == "remote.status.serving"
    assert status.serves(PANEL_PORT)
    assert status.url == PANEL_URL


# ------------------------------------------------------------- refusing to explode


def test_malformed_json_resolves_to_stopped() -> None:
    fake = FakeTailscale(status=CommandResult(stdout='{"BackendState": "Runn'))
    status = remote(fake).detect()

    assert status.state is RemoteState.STOPPED
    assert status.detail


def test_json_that_is_not_an_object_resolves_to_stopped() -> None:
    fake = FakeTailscale(status=CommandResult(stdout="[1, 2, 3]"))
    assert remote(fake).detect().state is RemoteState.STOPPED


def test_a_timeout_resolves_to_stopped() -> None:
    fake = FakeTailscale(status=CommandResult(returncode=-1, timed_out=True))
    status = remote(fake).detect()

    assert status.state is RemoteState.STOPPED
    assert "in time" in status.detail


def test_empty_output_resolves_to_stopped() -> None:
    fake = FakeTailscale(status=CommandResult(returncode=1, stderr="failed to connect"))
    assert remote(fake).detect().state is RemoteState.STOPPED


def test_a_runner_that_raises_never_reaches_the_caller() -> None:
    """The panel renders this on every page load, so nothing may propagate."""

    def exploding(argv: Sequence[str], timeout: float, /) -> CommandResult:
        raise RuntimeError("tailscaled ate the socket")

    status = TailscaleRemote(Config(), exploding).detect()
    assert status.state is RemoteState.STOPPED


def test_a_garbled_serve_config_leaves_the_node_merely_running() -> None:
    fake = FakeTailscale(serve_status=CommandResult(stdout='{"Web": "not a mapping"}'))
    status = remote(fake).detect()

    assert status.state is RemoteState.RUNNING
    assert status.served_ports == ()


def test_a_serve_handler_without_a_port_is_ignored() -> None:
    payload = json.dumps({"Web": {"host:443": {"Handlers": {"/": {"Proxy": "unix:/run/x"}}}}})
    fake = FakeTailscale(serve_status=CommandResult(stdout=payload))
    assert remote(fake).detect().served_ports == ()


# ---------------------------------------------------------------------- the funnel


def test_a_funnel_on_the_tailnet_port_makes_the_node_public() -> None:
    """`remote.only_tailnet` is a promise, so the panel is told when it is false."""
    status = remote(FakeTailscale(served_port=PANEL_PORT, funnel_port=443)).detect()

    assert status.public
    assert status.funnel_ports == (443,)


def test_a_funnel_on_another_port_is_not_our_problem() -> None:
    status = remote(FakeTailscale(served_port=PANEL_PORT, funnel_port=8443)).detect()

    assert not status.public
    assert status.state is RemoteState.SERVING


def test_no_funnel_is_the_normal_case() -> None:
    assert not remote(FakeTailscale(served_port=PANEL_PORT)).detect().public


def test_a_funnel_value_that_is_not_true_does_not_count() -> None:
    payload = json.dumps({"AllowFunnel": {f"{HOST}:443": "yes"}})
    fake = FakeTailscale(serve_status=CommandResult(stdout=payload))
    assert not remote(fake).detect().public


def test_a_funnel_key_is_read_whatever_shape_it_arrives_in() -> None:
    """A bare port still counts, and a key that is neither does not explode."""
    payload = json.dumps({"AllowFunnel": {"443": True, "²": True, "host:nope": True}})
    fake = FakeTailscale(serve_status=CommandResult(stdout=payload))
    assert remote(fake).detect().public


def test_a_foreground_serve_session_counts_as_published() -> None:
    """`tailscale serve` without --bg keeps its config under Foreground, and it
    publishes every bit as much as a backgrounded one does."""
    payload = json.dumps(
        {
            "Foreground": {
                "sess-1": {
                    "Web": {
                        f"{HOST}:443": {
                            "Handlers": {"/": {"Proxy": f"http://127.0.0.1:{PANEL_PORT}"}}
                        }
                    },
                    "AllowFunnel": {f"{HOST}:443": True},
                }
            }
        }
    )
    fake = FakeTailscale(serve_status=CommandResult(stdout=payload))
    status = remote(fake).detect()

    assert status.state is RemoteState.SERVING
    assert status.public


def test_enable_refuses_to_publish_through_an_open_funnel() -> None:
    """Funnel would put a child's control surface on the open internet."""
    fake = FakeTailscale(served_port=None, funnel_port=443)
    result = remote(fake).enable(PANEL_PORT)

    assert not result.ok
    assert result.error == "remote.no_port_forward"
    assert "funnel" in result.detail
    # Refused before anything was published, not undone afterwards.
    assert not any("--bg" in command for command in fake.commands())
    assert fake.served_port is None


# ----------------------------------------------------------------------- panel URL


def test_panel_url_comes_from_the_magic_dns_name() -> None:
    assert remote(FakeTailscale()).panel_url() == PANEL_URL


def test_panel_url_falls_back_to_hostname_plus_tailnet_suffix() -> None:
    payload = json.dumps(
        {
            "BackendState": "Running",
            "MagicDNSSuffix": "tail1a2b3c.ts.net",
            "Self": {"HostName": "biokids-vm"},
        }
    )
    fake = FakeTailscale(status=CommandResult(stdout=payload))
    assert remote(fake).panel_url() == PANEL_URL


def test_panel_url_is_none_for_a_name_that_is_not_a_tailnet_name() -> None:
    """Only MagicDNS names become a link we invite a parent to open."""
    fake = FakeTailscale(
        status=CommandResult(stdout=status_json(dns_name="biokids.example.com."))
    )
    assert remote(fake).panel_url() is None


def test_panel_url_is_none_when_the_node_is_not_running() -> None:
    assert remote(FakeTailscale(backend="Stopped")).panel_url() is None


def test_panel_url_is_none_when_the_binary_is_missing() -> None:
    fake = FakeTailscale(status=CommandResult(missing=True))
    assert remote(fake).panel_url() is None


# ------------------------------------------------------------------ enable/disable


def test_enable_runs_serve_bg_and_confirms_the_result() -> None:
    fake = FakeTailscale(served_port=None)
    result = remote(fake).enable(PANEL_PORT)

    assert result.ok
    assert result.error == ""
    assert result.status is not None
    assert result.status.state is RemoteState.SERVING
    assert "tailscale serve --bg 7878" in fake.commands()


def test_enable_defaults_to_the_configured_panel_port() -> None:
    fake = FakeTailscale(served_port=None)
    assert remote(fake).enable().ok
    assert fake.served_port == PANEL_PORT


def test_enable_refuses_when_tailscale_is_not_signed_in() -> None:
    fake = FakeTailscale(backend="NeedsLogin")
    result = remote(fake).enable(PANEL_PORT)

    assert not result.ok
    # The state is the message: "Tailscale is installed but not signed in".
    assert result.error == "remote.status.logged_out"
    assert not any("--bg" in command for command in fake.commands())


def test_enable_refuses_when_tailscale_is_absent() -> None:
    fake = FakeTailscale(status=CommandResult(missing=True))
    result = remote(fake).enable(PANEL_PORT)

    assert not result.ok
    assert result.error == "remote.status.absent"


def test_enable_reports_failure_when_the_command_changes_nothing() -> None:
    fake = FakeTailscale(
        served_port=None,
        action=CommandResult(returncode=1, stderr="serve: permission denied"),
        obeys_actions=False,
    )
    result = remote(fake).enable(PANEL_PORT)

    assert not result.ok
    assert result.error == RemoteError.COMMAND_FAILED
    assert "exited 1" in result.detail


#: What `tailscale serve --bg 7878` printed on the VM, on a tailnet one day old,
#: before it sat waiting for somebody to open the page (D-157).
SERVE_NOT_ENABLED = (
    "Serve is not enabled on your tailnet.\n"
    "To enable, visit:\n"
    "\n"
    "         https://login.tailscale.com/f/serve?node=nH6HrHEzpo11CNTRL\n"
)


def test_enable_hands_the_parent_tailscales_approval_page() -> None:
    """Every new tailnet has Serve off. The panel said only "could not be changed"."""
    fake = FakeTailscale(
        served_port=None,
        action=CommandResult(returncode=-1, stdout=SERVE_NOT_ENABLED, timed_out=True),
        obeys_actions=False,
    )
    result = remote(fake).enable(PANEL_PORT)

    assert not result.ok
    assert result.error == RemoteError.NEEDS_APPROVAL
    assert result.approval_url == "https://login.tailscale.com/f/serve?node=nH6HrHEzpo11CNTRL"


@pytest.mark.parametrize(
    "text",
    [
        "https://evil.example/f/serve?node=abc",
        "https://login.tailscale.com.evil.example/f/serve?node=abc",
        "http://login.tailscale.com/f/serve?node=abc",
        "https://login.tailscale.com/a/abc",
        "",
    ],
)
def test_only_a_tailscale_approval_page_is_ever_offered(text: str) -> None:
    """The panel shows this as a button; it must not be anybody else's link."""
    assert approval_url_in(text) == ""


def test_the_real_runner_keeps_what_a_command_said_before_it_timed_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`serve` prints the page and then waits; the timeout used to throw both away."""

    def slow(*_a: object, **_k: object) -> object:
        raise subprocess.TimeoutExpired(["tailscale"], 15.0, output=SERVE_NOT_ENABLED.encode())

    monkeypatch.setattr(remote_module.subprocess, "run", slow)
    result = run_command(["tailscale", "serve", "--bg", "7878"], 15.0)

    assert result.timed_out
    assert approval_url_in(result.stdout)


def test_enable_fails_when_the_mapping_takes_but_there_is_no_address() -> None:
    """Published with no MagicDNS name is nothing a parent can put on a phone."""
    fake = FakeTailscale(
        served_port=None,
        status=CommandResult(stdout=status_json(dns_name="biokids.example.com.")),
    )
    result = remote(fake).enable(PANEL_PORT)

    assert not result.ok
    assert result.error == RemoteError.COMMAND_FAILED
    # The log has to tell this apart from a command that did nothing at all.
    assert "MagicDNS" in result.detail


def test_enable_rejects_an_impossible_port_without_running_anything() -> None:
    fake = FakeTailscale()
    result = remote(fake).enable(70000)

    assert not result.ok
    assert result.error == RemoteError.BAD_PORT
    assert fake.calls == []


def test_disable_tears_the_mapping_down() -> None:
    fake = FakeTailscale(served_port=PANEL_PORT)
    result = remote(fake).disable(PANEL_PORT)

    assert result.ok
    assert result.status is not None
    assert result.status.state is RemoteState.RUNNING
    assert "tailscale serve --https=443 off" in fake.commands()


def test_disable_succeeds_when_nothing_was_published() -> None:
    fake = FakeTailscale(served_port=None)
    assert remote(fake).disable(PANEL_PORT).ok


def test_disable_reports_failure_when_the_mapping_survives() -> None:
    fake = FakeTailscale(served_port=PANEL_PORT, obeys_actions=False)
    result = remote(fake).disable(PANEL_PORT)

    assert not result.ok
    assert result.error == RemoteError.COMMAND_FAILED


def test_disable_is_not_reported_off_when_the_node_cannot_confirm_it() -> None:
    """A down node publishes nothing today and everything again after a reboot.

    The stored serve config outlives tailscaled, so a failed `serve off` against
    a node that is not up must not be read as "phone access is now off".
    """
    fake = FakeTailscale(
        backend="Stopped",
        served_port=PANEL_PORT,
        action=CommandResult(returncode=1, stderr="Tailscale is stopped."),
        obeys_actions=False,
    )
    result = remote(fake).disable(PANEL_PORT)

    assert not result.ok
    assert result.error == RemoteError.COMMAND_FAILED
    assert result.detail


def test_disable_succeeds_on_a_stopped_node_when_the_command_takes() -> None:
    """`tailscale serve off` edits the stored config even with the node down."""
    fake = FakeTailscale(backend="Stopped", served_port=PANEL_PORT)
    result = remote(fake).disable(PANEL_PORT)

    assert result.ok
    assert fake.served_port is None


def test_disable_refuses_when_tailscale_is_absent() -> None:
    fake = FakeTailscale(status=CommandResult(missing=True))
    result = remote(fake).disable(PANEL_PORT)

    assert not result.ok
    assert result.error == "remote.status.absent"


# -------------------------------------------------------------------- the real runner


def test_run_command_reports_a_missing_binary_as_a_state() -> None:
    result = run_command(["biokids-no-such-binary-4b21", "status"], 5.0)

    assert result.missing
    assert not result.ok


# --------------------------------------------------------------------------- the QR

SVG_NS = "http://www.w3.org/2000/svg"


def test_qr_svg_renders_a_scannable_svg_document() -> None:
    svg = qr_svg(PANEL_URL)
    root = ElementTree.fromstring(svg)

    assert root.tag == f"{{{SVG_NS}}}svg"
    assert root.get("viewBox")
    # One path of modules on a white ground: exactly what a scanner wants.
    assert root.find(f"{{{SVG_NS}}}rect") is not None
    assert root.find(f"{{{SVG_NS}}}path") is not None


def test_qr_svg_carries_no_script_and_nothing_to_fetch() -> None:
    svg = qr_svg(PANEL_URL).lower()

    assert "<script" not in svg
    # Nothing to load: no CDN, no external image, no font.
    assert "href" not in svg
    assert "<image" not in svg
    # The address is encoded into the modules, never written into the markup.
    assert "ts.net" not in svg


def test_qr_svg_grows_with_the_data_it_encodes() -> None:
    short = qr_svg("https://a.b.ts.net")
    long = qr_svg("https://a.b.ts.net/" + "x" * 200)

    assert len(long) > len(short)


def test_qr_svg_refuses_an_empty_url() -> None:
    with pytest.raises(ValueError, match="URL"):
        qr_svg("   ")


# -------------------------------------------------------------------------- i18n


def test_every_state_and_error_key_exists_in_both_catalogs() -> None:
    keys = [state.i18n_key for state in RemoteState]
    keys += [RemoteError.COMMAND_FAILED, RemoteError.BAD_PORT]
    keys += [
        RemoteCopy.ENABLE,
        RemoteCopy.DISABLE,
        RemoteCopy.URL_LABEL,
        RemoteCopy.QR_HELP,
        RemoteCopy.INSTALL_HELP,
        RemoteCopy.LOCALHOST_ONLY,
        RemoteCopy.NO_PORT_FORWARD,
        RemoteCopy.ONLY_TAILNET,
    ]

    for locale in ("tr", "en"):
        catalog = load_catalog(locale)
        missing = [key for key in keys if not catalog.has(key)]
        assert missing == [], f"{locale} is missing {missing}"
