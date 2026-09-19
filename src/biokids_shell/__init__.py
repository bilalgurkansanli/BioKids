"""The child launcher.

A small web app the child sees instead of a desktop (D-14, D-95). It runs as the
child user with no privilege of its own: it cannot read the database, cannot
change a policy, and cannot add a second to anyone's day. Everything it draws
comes from the daemon over `/run/biokids/shell.sock`, and everything it is asked
to start it asks the daemon about first.

That is the point. The launcher is the surface a determined seven-year-old will
spend an afternoon poking at, so it is built so that poking at it successfully
achieves nothing.
"""

from __future__ import annotations

__all__: list[str] = []
