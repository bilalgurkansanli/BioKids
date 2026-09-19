"""The optional on-device supervision worker.

Deliberately empty. `docs/AI-LAYER.md` is the specification and Phase 2b is when
it gets written; what matters now is the constraint it has to fit (D-82):

    Optional package, default off, `Suggests:` never `Depends:`. Everything in
    BioKids works without it. If this component is absent, broken, or disabled,
    the rest of the system behaves exactly as if it had never existed.

That is already true, and this file is what keeps it verifiable: nothing in
`biokids_core`, `biokids_daemon`, `biokids_panel` or `biokids_shell` imports
this package, and the policy engine's AI branch is inert unless a score arrives
with a threshold to compare it against.

The file also has to exist for the packaging: `debian/biokids-ai.install` claims
`usr/lib/python3/dist-packages/biokids_ai`, and `dh_install` fails the whole
build on a glob that matches nothing. See `src/biokids_ai/README.md`.
"""

from __future__ import annotations

__all__: list[str] = []
