# `biokids-ai`

**Phase 2b. Nothing here yet, and that is the design.**

`docs/AI-LAYER.md` is the full specification. The thing to understand before writing
any of it is the constraint it operates under (D-82):

> Optional package, default off, `Suggests:` never `Depends:`. Everything in BioKids
> works without it. If this component is absent, broken, or disabled, the rest of the
> system behaves exactly as if it had never existed.

That is already true: nothing in `biokids_core`, `biokids_daemon`, `biokids_panel` or
`biokids_shell` imports this package, and the policy engine's AI branch is inert unless
a score arrives with a threshold to compare it against.

## The shape it has to fit

The daemon emits triggers; this worker subscribes and never polls (D-70, D-77). It
answers with a 0–100 risk score, a category and a short reason (D-78). **It never
enforces anything** — the policy engine decides what a score means for a child of that
age, and `docs/AI-LAYER.md` §7's bands say the middle band informs and does not punish.

## Before writing any of it

Verify the CUDA toolchain first. `docs/AI-LAYER.md` §5: the development machine has an
RTX 5050 (Blackwell, `sm_120`), which needs CUDA 12.8 or newer and a llama.cpp build
that knows about it. If that fights back, develop against Moondream2 on CPU and revisit
— a toolchain problem must not stall the rest of the phase. VirtualBox has no GPU
passthrough, so this component cannot be meaningfully tested in the development VM at
all.
