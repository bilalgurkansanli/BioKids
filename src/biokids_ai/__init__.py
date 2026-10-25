"""Sor Bana: the child's local assistant (D-165), and English with Sio (D-226).

Optional and off until a parent turns it on for a child; nothing else in
BioKids imports this package. It runs as its own service, as the `biokids-ai`
user with no network (`PrivateNetwork=yes`), and answers the daemon over a Unix
socket. `src/biokids_ai/README.md` says how a question becomes an answer.
"""

from __future__ import annotations

__all__: list[str] = []
