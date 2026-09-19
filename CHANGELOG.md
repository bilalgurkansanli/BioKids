# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and the project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Versions stay in `0.x` until Phase 1 is complete (`docs/PACKAGING.md` §1). Until then a
minor bump may change the database schema, the config file format or the IPC vocabulary;
each such change is listed here with what it means for an existing installation.
`1.0.0` is the point where a stranger can install the ISO and expect to be supported
afterwards.

## [Unreleased]

Phase 0 — the BioGuard MVP — is in progress. Nothing has been released or tagged yet;
the exit criterion is in `docs/ROADMAP.md` and `v0.1.0` is tagged when it is met
(`docs/TASKS-PHASE0.md` T9). `pyproject.toml` already carries `0.1.0`: that is the
version being built towards, not one that has shipped. What follows is what has landed
so far.

### Added

- **Repository scaffold.** The layout from `CLAUDE.md` §4, the MIT licence, and
  `pyproject.toml` carrying the ruff, `mypy --strict` and pytest configuration in one
  place. GitHub issue templates (bug, feature, hardware compatibility) and a pull request
  checklist that includes the i18n, decisions and outbound-network rules.
- **CI.** One workflow on every push and pull request: `ruff check`, `ruff format
  --check`, `mypy --strict` over core and daemon, the translation catalog check, pytest
  with coverage, a dependency licence allowlist enforcing D-30, and a `.deb` build inside
  a `debian:trixie` container gated on `lintian --fail-on error`.
- **`biokids-core` configuration.** A loader for `/etc/biokids/config.toml` with schema
  validation and defaults, so every path and port from `CLAUDE.md` §5 is a value rather
  than a literal. `BIOKIDS_PREFIX` re-roots the entire runtime layout, which is what lets
  the suite and a development panel run without root.
- **Database schema.** SQLAlchemy models for `docs/ARCHITECTURE.md` §5 plus the
  boot-epoch, ledger-day and panel-session tables that section implies, with Alembic and
  an initial migration. SQLite in WAL mode, `foreign_keys=ON`, mode 0600 on the file.
- **Internationalisation.** gettext catalogs for `tr` and `en` with the Turkish written
  first, a `_()` helper, and `dev/check_i18n.py`, which fails the build when a key exists
  in one locale but not the other or when a translation is left empty.
- **Policy engine.** `PolicyEngine.evaluate()` implementing the precedence rules of
  `docs/ARCHITECTURE.md` §4 — pure, with no I/O and no clock access; the wall-clock time
  is a parameter.
- **Time ledger.** `CLOCK_BOOTTIME` accounting with boot-epoch detection and carry-over,
  suspended time derived from the BOOTTIME-minus-MONOTONIC delta rather than trusted from
  logind, an unexplained gap clamped instead of billed, and wall-clock jumps written to
  the audit log without ever adjusting the balance. Moving the system clock neither
  creates nor destroys quota.
- **Security primitives.** argon2id hashing for the PIN and the passphrase
  (`m=64MiB, t=3, p=4`), an eight-word recovery code, and encryption at rest for the SMTP
  password.
- **IPC protocol.** Line-delimited JSON over `/run/biokids/daemon.sock`, with the
  operation and event vocabulary the packages share and both a blocking and an asyncio
  client.
- **Repository layer.** The single place stored rows become the engine's frozen
  `ChildPolicy`, so a schema change breaks in one file instead of four.
- **App catalog.** Applications as data (D-20), synchronised into the database
  additively so that an upgrade which drops an application does not lose a family's
  arrangement of tiles.
- **Tests.** The `docs/TASKS-PHASE0.md` T2.5 ledger list in full — an hour of use costs
  an hour, three hours of clock movement costs nothing, a reboot carries the total
  forward, a crash costs at most one persist interval, suspend and a locked screen cost
  nothing, two sessions for one child are counted once — plus every precedence rule and
  edge case of the policy engine.
- **Project hygiene.** `CONTRIBUTING.md`, including the explicit list of features this
  project will not accept (`docs/SECURITY.md` §7); `CODE_OF_CONDUCT.md`; a root
  `SECURITY.md` disclosure policy; and this changelog.

No telemetry, analytics, crash reporting or version ping has been added, and none will
be. The complete inventory of outbound connections is `docs/SECURITY.md` §4.

Two placeholders are outstanding before the repository can be made public: the security
contact in `SECURITY.md` and the enforcement contact in `CODE_OF_CONDUCT.md`.

[Unreleased]: https://github.com/bilalgurkansanli/biokids/commits/main
