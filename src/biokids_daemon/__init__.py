"""The enforcement daemon.

The only component allowed to change system state. The panel, the shell and the
voice service all ask it; none of them lock a session, stop a process or power a
machine off themselves.

Everything here is wiring. The rules live in `biokids_core.policy` and the
numbers live in `biokids_core.ledger`, both of which are pure and tested without
a machine. What is left in this package is the part that genuinely needs a
running system: logind, a unix socket, `systemctl`, and a loop.
"""

from __future__ import annotations

__all__: list[str] = []
