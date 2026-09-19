## What this changes

<!-- One paragraph. What a parent or a child would notice, if anything. -->

## Checklist

- [ ] Tests for logic that can be wrong (ledger, policy engine, scorer, parsers)
- [ ] `ruff check`, `ruff format --check` and `mypy` pass locally
- [ ] Every user-facing string goes through i18n, in both `tr` and `en`
- [ ] `docs/` updated in the same commit if this changed how something works
- [ ] `docs/DECISIONS.md` updated if this reverses or adds a decision
- [ ] No new outbound network call, or one added to `docs/SECURITY.md` §4 with a decision entry
- [ ] Nothing on the won't-build list in `docs/SECURITY.md` §7

## How it was tested

<!-- VM, host, unit tests, manual steps. "It builds" is not a test. -->
