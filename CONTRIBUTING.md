# Contributing to BioKids

**Türkçe okuyanlara:** BioKids Türkçe düşünülerek yazılıyor; arayüzün ilk dili Türkçe,
İngilizcesi sonra geliyor. Issue ve tartışmaları rahatça Türkçe açabilirsiniz, cevabını
da Türkçe alırsınız. Tek kural depoya giren şeylerle ilgili: kod, kod içindeki yorumlar,
commit mesajları ve `docs/` altındaki belgeler İngilizce yazılır (D-119). Bu bir tercih
meselesi değil; projeye dışarıdan bakan birinin de okuyabilmesi için.

---

## Before you write anything

Read [`CLAUDE.md`](CLAUDE.md). It is short and it is the project constitution: six
principles that override everything else, and a table of which document to open for
which area. Then read the doc that matches what you are about to touch.

Then read [the list of features this project will not accept](#features-this-project-will-not-accept),
further down. It is there so nobody spends a weekend on a pull request that was never
going to be merged.

---

## Development setup

### The VM

**Development happens in a VirtualBox VM running Debian 13 "Trixie", never on bare
metal** (`CLAUDE.md` §6, D-115). BioKids installs systemd units, rewrites nftables
rules, changes what logind does with a session, and can set a GRUB password. On your own
machine, a bug in any of those costs you a working desktop for an evening.

- 4 GB RAM, 2 vCPU, 30 GB disk, the XFCE task, Guest Additions, and a shared folder to
  your checkout on the host.
- Snapshot the VM as `clean-debian-13` **before** installing anything of ours, and roll
  back to it for every install test. A package that only works on a machine that already
  had the previous version is not a package that works.
- Snapshot again before anything that touches boot, PAM, nftables or GRUB.

Real hardware is only needed for wifi and GPU drivers, Secure Boot, installer
partitioning, and GPU inference performance (D-116). The AI layer in particular cannot
be tested in VirtualBox — there is no GPU passthrough — so run `biokids-ai` on a host
with a GPU when you get to it.

### Python

Debian 13 ships Python 3.13 and that is the only version supported.

```bash
git clone https://github.com/bilalgurkansanli/BioKids
cd biokids
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,daemon]"
```

The `daemon` extra pulls `dbus-next`, which is Linux-only. On a non-Linux host install
`.[dev]` alone and skip the tests marked `linux_only`.

### Running as an ordinary user

Core reads `/etc/biokids/config.toml` and writes under `/var/lib/biokids`, both of which
need root. Set `BIOKIDS_PREFIX` to re-root the whole layout somewhere writable, and you
can run the test suite and a development panel without `sudo`:

```bash
export BIOKIDS_PREFIX=~/.local/share/biokids-dev
```

`BIOKIDS_CONFIG` points at one specific config file if you need to override the path
rather than the prefix.

---

## Checks

These are what CI runs, in the same order. Run them before you open a pull request.

```bash
ruff check src tests dev
ruff format src tests dev   # CI runs --check and fails rather than rewriting
mypy                        # configured in pyproject: --strict over core and daemon
python dev/check_i18n.py    # translation catalogs must not drift apart
pytest
```

- On a non-Linux host: `pytest -m "not linux_only"`.
- One suite while iterating: `pytest tests/test_ledger.py`.
- `python dev/check_licenses.py` fails if a dependency's licence is outside the
  allowlist (MIT, BSD, Apache-2.0, ISC, PSF). It reads installed metadata through
  `pip-licenses`, which the `dev` extra does not pull in, so `pip install pip-licenses`
  first — that is what CI's separate licence job does. Run it whenever you touch
  dependencies.

Tests are required for logic that can be wrong — the time ledger, the policy engine, the
risk scorer, the category allocator, the earned-time calculator (D-118). Templates and
CSS are reviewed by looking at them. The ledger suite in particular is the backbone of
the product: if you change how time is counted, the existing tests must still pass for a
reason you can explain, not because you adjusted them.

---

## Rules a pull request is measured against

**Types.** Type hints everywhere, not only in the two packages `mypy --strict` covers.

**Translations.** Every user-facing string goes through `biokids_core.i18n`. Never
hardcode Turkish or English in a template or a Python file. A key must exist in both
`src/biokids_core/locale/tr/LC_MESSAGES/biokids.po` and the `en` catalog, and neither may
be empty — `dev/check_i18n.py` fails the build otherwise. Write the Turkish first and
make it sound like Turkish. A string that reads like a translated English sentence is a
bug report waiting to happen from a parent who thinks the software was not made for them.
There is no compile step in the edit loop: the `.po` is read directly when no `.mo` is
present, and `dev/compile_i18n.py` produces the binary catalogs during the package build.

**Dependencies.** Prefer the standard library. A new third-party dependency needs a
reason in the pull request description. Never add a GPL-3 library to a package we ship
code with; talking to a GPL-3 program in a separate process over DBus, HTTP or a CLI is
fine, and gets documented.

**Assets.** `dev/check_licenses.py` only ever sees Python packages. A bundled Piper
voice, a model, a font or an icon carries terms too, and parts of the Piper catalog are
not redistributable at all (`docs/VOICE.md` §3). Record what you bundled, where it came
from and under which licence — voices go in `assets/voices/LICENSES.md` — and if it
cannot be redistributed, download it at first run instead of shipping it.

**Network.** `docs/SECURITY.md` §4 is the complete inventory of outbound connections
BioKids makes. Code that contacts anything not in that table will not be merged. Adding
a row means a decision entry in `docs/DECISIONS.md` and a note in the README, written
before the code, not after it.

**Failing safe.** When something the enforcement path depends on is missing or broken —
the daemon is gone, the config will not parse, the filter will not reload, an IPC call
times out — the child session locks and the parent is told (`CLAUDE.md` principle 3).
Logging a warning and continuing is the one behaviour this project cannot ship. An
`except` that swallows an error anywhere on that path is a bug even when the surrounding
code is right, and a review will ask what happens if the call inside it never returns.

**Not bricking the machine.** Anything that can lock a parent out of their own computer
— a GRUB password, recovery mode, an nftables ruleset, a PAM or logind change — is
opt-in, undone by the same tool that did it, and documented with a recovery path that
works from a live USB (principle 4). BioKids does not touch the bootloader or the
firmware at all (D-184); SECURITY.md section 6 tells a family how to, by hand. New work
in that territory arrives with a way to check, apply and undo it, or it does not arrive.

**Packaging.** Maintainer scripts run more than once — on upgrade, on reinstall, and
after one that failed halfway. Every one of them has to be safe to re-run, and must not
rewrite a config the admin may have edited without `ucf`. `remove` never deletes
`/var/lib/biokids`; only `purge` does, and only after asking through debconf
(`docs/PACKAGING.md` §1). Generated files — dnsmasq snippets, Chromium policies,
nftables rules — are rendered from the database, are not conffiles, and say so in a
header comment.

**Comments.** Explain why, not what. If a piece of code looks strange and is correct,
say what would go wrong if it were written the obvious way.

**Docs.** When a spec turns out to be wrong — and some of it will — fix the doc in the
same commit as the code. A spec that has drifted from the code is worse than no spec.

**Data.** Never commit a database, a log containing a real child's name, a file from
`/var/lib/biokids/evidence/`, or a screenshot of a real child's session. `.gitignore`
covers the obvious paths. It cannot cover a screenshot you drag into an issue.

---

## Commits

Conventional Commits, in English (D-119):

```
feat(daemon): add boottime quota ledger
fix(panel): keep the PIN lockout across a restart
docs(security): list the DoH bootstrap hosts we drop
```

Types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `build`, `ci`, `perf`.
Scopes: `core`, `daemon`, `panel`, `shell`, `voice`, `ai`, `extension`, `packaging`,
`docs`, `ci`.

Release notes are generated from these subject lines, so strangers read them. Where a
change is visible to a parent or a child, say what they will notice. A breaking change
takes a `!` after the scope and a `BREAKING CHANGE:` footer explaining the migration —
for a tool that holds a family's history, "breaking" includes the database schema, the
config file format and the IPC vocabulary.

---

## Branches and pull requests

Standard open source flow (D-121): feature branches, pull requests into `main`, tagged
releases. Not sequential version branches, not a long-lived release branch per version.

- Branch names: `feat/...`, `fix/...`, `docs/...`
- One pull request per coherent unit — a module, a screen, a package. Not one per file,
  and not one that rewrites three subsystems at once.
- `main` must always build. Branch protection requires CI green and one review, which
  may be a self-review while the project has one maintainer.
- Fill in the checklist in the pull request template honestly. "It builds" is not a
  test, and an unticked box with a sentence explaining why is fine — a ticked box that
  is not true is not.
- Releases are tagged `vX.Y.Z`. Versions stay in `0.x` until Phase 1 is complete.

---

## Features this project will not accept

Some of these are easy to build and some get asked for. They are refused on purpose, and
the reason is always the same one: BioKids supervises a child **in the open**, for a
parent who is in the same house, and it is not a surveillance tool. Principle 2 of
`CLAUDE.md` is "the child knows". `docs/SECURITY.md` §7 is where this list lives in the
specification; here is what each item costs and why it stays out.

- **Covert monitoring.** Supervision the child cannot see teaches them that the computer
  might be watching in ways nobody told them about, which is exactly the instinct that
  makes a child hide a problem instead of reporting it. A parent may make the on-screen
  indicator smaller; the feature is never covert by design.

- **Hidden or "stealth" install modes.** A build that hides its own presence works just
  as well on an adult's machine as on a child's, and the person installing it is not
  always the person the tool describes. We are not going to ship something whose main
  feature is that the person using the computer cannot tell it is there.

- **Keystroke logging.** It captures everything — a password, a message to a friend, a
  search about something the child is frightened to ask a grown-up — in order to answer
  a question that time limits, a filter and a conversation answer better. It also turns
  the machine into the most valuable thing in the house to an attacker.

- **Microphone or camera capture.** Recording a child's room is a different product with
  a different threat model, and it creates a file that is catastrophic if it ever leaks.
  Nothing in the supervision BioKids offers gets meaningfully better for having audio or
  video of the child in front of the screen. The one exception is D-166: the child may
  hold a button on the assistant screen and ask a question aloud, transcribed on the
  machine and thrown away. Background listening, wake words and keeping audio are not
  part of that exception.

- **Hidden or recorded viewing of a child's screen.** The owner added one live view
  (D-253): a parent presses "Canlı izle" in the panel, the child's screen says who is
  watching for as long as it lasts, about one small picture a second goes from the
  child's own session to the panel's page, and none is kept anywhere. A view the child
  is not shown, a recording, a history of screens, or a view that runs without a
  parent's page asking for it, will not be accepted.

- **Location tracking.** BioKids protects one computer in one house (`docs/SECURITY.md`
  §1). A location answers no question inside that scope, and a child's location is the
  single most dangerous field anyone could store about them.

- **Reading the contents of private messages.** Reading a child's conversations teaches
  them to move the conversation somewhere you cannot see, so it fails on its own terms.
  It also puts the other person — usually another child, who agreed to nothing — under
  surveillance by a tool their family never installed.

The adjacent things we **do** build, so the boundary is legible: a visible supervision
indicator; event-triggered screenshot scoring that happens in RAM on the local GPU and
writes only a flagged frame to `/var/lib/biokids/evidence/`, with retention and a parent
view; a report of which addresses were requested and which were blocked; and spoken
warnings before time runs out. The working test is simple — if you could show the entire
feature to the child and it would still do its job, it belongs here.

If you think one of these is wrong, open an issue before writing code. That is a
conversation about the product, and it is worth having; it is just not worth having as a
review comment on a finished patch.

---

## Security issues

Do not open a public issue. See [`SECURITY.md`](SECURITY.md) in the repository root for
how to report privately.

---

## Code of conduct

Participation in this project is governed by [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).

---

## Licence

MIT. By contributing, you agree that your contribution is licensed under it.
