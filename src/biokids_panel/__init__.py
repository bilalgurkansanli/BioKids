"""The parent panel.

FastAPI on `127.0.0.1:7878`, Jinja2 templates, hand-written CSS, no build step
and no CDN (D-32). Remote access is `tailscale serve` and nothing else (D-33):
the panel never terminates TLS and never listens on a LAN address, because
someone would then forward a port to it.

The panel reads the database directly for queries and goes through the daemon
socket for every mutation that affects a running child, so the daemon stays the
single writer of live policy (`ARCHITECTURE.md` §2).
"""

from __future__ import annotations

__all__: list[str] = []
