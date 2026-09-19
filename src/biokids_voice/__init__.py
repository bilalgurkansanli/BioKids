"""Zio and Sio — the two spoken companions.

A small service that turns an event key into a warm sentence, speaks it, and
tells the shell when it has finished so the character card can slide away. It
runs as a **user** unit in the child's session (`CLAUDE.md` §5), which shapes
almost every decision in this package: it cannot open the root-owned database,
it cannot lock a session, and it cannot be trusted with anything the daemon
owns. All it does is talk.

The pipeline is four steps, one module each:

    event key ──lines.py──> a sentence
                            │
                            └──synth.py──> a WAV, cached
                                           │
                                           └──player.py──> audio, ducked
                                                           │
                                       service.py ─────────┘  socket, queue,
                                                              quiet hours,
                                                              finished events

Every step degrades rather than fails. No cloned voice falls back to stock Piper,
no Piper falls back to the card alone, and the card alone is still a success as
far as the shell is concerned — VOICE.md §6 is explicit that a child must never
be left with silence *and* no visual. One-way only, always: there is no
microphone in the child session and no speech recognition anywhere (D-92).
"""

from __future__ import annotations

__all__: list[str] = []
