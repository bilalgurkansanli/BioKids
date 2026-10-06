"""Phone access, one button, from the settings screen and wizard step 8 alike (D-169).

Pressing "Telefondan erişimi aç" used to work only on a machine where someone
had already installed Tailscale and signed in at a terminal. Now the button
looks at where Tailscale is: not installed, signed out or stopped, it starts
`biokids-remote-setup.service` (through the daemon, like the assistant's
download) and the screen follows its progress; up, it publishes the panel at
once, as before.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from sqlalchemy.orm import Session

from biokids_core import remote, repo
from biokids_core.errors import IpcError
from biokids_core.ipc import Op
from biokids_panel.context import PanelContext

log = logging.getLogger(__name__)

Outcome = Literal["on", "setup", "approval", "error"]

NEEDS_SETUP = frozenset(
    {remote.RemoteState.ABSENT, remote.RemoteState.LOGGED_OUT, remote.RemoteState.STOPPED}
)


def view(ctx: PanelContext, session: Session) -> dict[str, Any]:
    """What the phone-access card needs to draw itself."""
    status = remote.detect()
    return {
        "tailscale": status,
        "panel_port": ctx.config.panel.port,
        "panel_url": status.url,
        "qr": remote.qr_svg(status.url) if status.url else "",
        "auth_qr": remote.qr_svg(status.auth_url) if status.auth_url else "",
        "setup_state": remote.setup_state(),
        "approval_url": remote.approval_url_in(
            repo.get_setting(session, remote.SETTING_APPROVAL_URL)
        ),
    }


def turn_on(ctx: PanelContext, session: Session, actor: str) -> Outcome:
    """Publish the panel now, or start the setup that will."""
    status = remote.detect()
    if status.state in NEEDS_SETUP:
        try:
            started = ctx.client.call(Op.REMOTE_SETUP).ok
        except (IpcError, OSError) as exc:
            log.warning("the daemon did not start the phone-access setup: %s", exc)
            started = False
        repo.audit(
            session,
            actor=actor,
            action="remote_setup_started",
            detail={"from": status.state.value, "ok": started},
            severity="info" if started else "warning",
        )
        repo.set_setting(session, remote.SETTING_APPROVAL_URL, "")
        session.commit()
        return "setup" if started else "error"

    result = remote.enable(ctx.config.panel.port)
    if not result.ok:
        log.warning("remote access could not be enabled: %s (%s)", result.error, result.detail)
    repo.audit(
        session,
        actor=actor,
        action="remote_access_changed",
        detail={
            "enabled": True,
            "ok": result.ok,
            "error": result.error,
            "detail": result.detail[:300],
        },
        severity="info" if result.ok else "warning",
    )
    repo.set_setting(session, remote.SETTING_APPROVAL_URL, result.approval_url)
    session.commit()
    if result.ok:
        return "on"
    return "approval" if result.approval_url else "error"
