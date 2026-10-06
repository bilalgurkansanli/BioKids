# BioKids

**A Debian-based Linux system for children aged 4–10, with parental controls that run
entirely on your own machine.**

*Çocuklar için, tamamen kendi bilgisayarınızda çalışan ebeveyn kontrollü Linux sistemi.*

> ⚠️ **Early development — Phase 0.** The daemon, the parent panel, the child
> launcher and the voice service are built and tested — 541 tests, `mypy --strict`,
> and an acceptance test that runs the exit criterion end to end in one process.
> But **nothing has ever been built as a `.deb` or installed on any machine**: the
> development host is Windows, so `dpkg-buildpackage` has never run. There is no
> release and no tag. See [`docs/ROADMAP.md`](docs/ROADMAP.md) for what is done,
> and [`docs/VM-RUNBOOK.md`](docs/VM-RUNBOOK.md) for exactly what is left.

---

## What it does

- **Time limits** that a child cannot cheat by changing the clock. Daily quotas, time
  windows, separate weekday and weekend rules, spoken warnings before the machine
  shuts down.
- **Content filtering.** The internet is open with adult, gambling and harmful sites
  always closed, by a DNS filter on the machine that applies to the children's
  accounts only; a parent can switch a child to "only the sites I choose" instead.
  SafeSearch is forced on Google, YouTube, Bing and DuckDuckGo.
- **"Sor Bana", a question box for the child**, answered by a language model running
  on the machine, for the child's age. It refuses what a child should not be asking
  and tells the parent; the child is told that on the screen. Off by default; the
  child can also ask aloud, by holding a button, if the parent allows it.
- **A child launcher** instead of a desktop. Big tiles, no file system, no settings, no
  terminal. The interface adapts to the child's age.
- **Earned time.** Time spent in educational applications turns into extra playtime, at
  a ratio the parent sets.
- **Zio and Sio**, two voice companions who announce warnings and rewards. Optionally,
  they speak in the parents' own recorded voices, cloned locally.
- **Optional AI supervision** that looks at the screen when something new happens,
  scores how appropriate it is for the child's age, and tells the parent. The model
  runs on your GPU; no screenshot ever leaves the machine.
- **A parent panel** in the computer's applications menu, and on your phone over your
  own private network: one button installs and signs in Tailscale for you. No
  BioKids account, no cloud, no port forwarding.

### The parent panel

![The Today screen: one card per child, with the time remaining as a ring, what is on
screen right now, and three large buttons](assets/screenshots/panel-today.png)

One card per child and nothing else. A parent glancing at this on their phone has to
understand it in three seconds without reading a label twice, so it is a ring, one line
of state, and **+10 dk**, **Kilitle**, **Kapat**. Everything else is one level down.

![The time rules for one child: weekday and weekend quotas, allowed hours, and what
happens when the time runs out](assets/screenshots/panel-child-time.png)

Five visible controls. Forced breaks and earned time live behind *Gelişmiş*, because
most parents never touch them and a screen of thirty inputs is a screen nobody
finishes.

![The audit log, filtered by child, action and date](assets/screenshots/panel-audit.png)

Every rule change, every PIN attempt, every warning spoken and every time the system
clock moved. Append-only; the application never deletes from it.

---

## Privacy

**Nothing leaves your device.** No telemetry, no analytics, no crash reporting, no
account. Screenshots are analysed in memory on your own GPU and discarded. Voice
recordings never leave the computer.

The complete list of outbound connections BioKids makes — and there are only eight, most
of them optional — is in [`docs/SECURITY.md`](docs/SECURITY.md#4-outbound-network-inventory).

---

## What it does not do

BioKids is built for a **curious child aged 4 to 10, working alone on one computer.**

It will not stop someone who boots from a USB stick, edits the kernel command line,
removes the drive, or has an adult helping them. It does not protect phones or tablets.
It is a supervision aid, not a guarantee — no filter catches everything, and none of
this replaces talking to your child.

The full list of what is in and out of scope is in
[`docs/SECURITY.md`](docs/SECURITY.md#1-threat-model). We would rather be honest about
the limits than let a parent feel safer than they are.

---

## How it fits together

```
  parent's phone ──(tailscale serve)──▶  biokids-panel   ─┐
                                          127.0.0.1:7878  │  /run/biokids/daemon.sock
                                                          ▼
                                                    biokids-daemon        (root)
                                                    · BOOTTIME ledger
                                                    · policy engine
                                                    · logind watcher
                                                    · enforcer
                                                          ▲
                                          127.0.0.1:7879  │  /run/biokids/shell.sock
  the child      ──────────────────────▶  biokids-shell  ─┘   (authorised by peer uid)
                    kiosk Chromium        biokids-voice
```

The daemon is the only component allowed to change system state; everything else asks
it. The panel and the launcher reach it over two different sockets with two very
different sets of permissions — the child's session cannot grant time, unlock a screen
or reach a sibling's data. [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) has the
detail.

Three things carry most of the correctness:

- **Time accounting never touches the wall clock.** Every interval is a difference of
  two `CLOCK_BOOTTIME` readings, minus the part the machine spent suspended — which is
  derived by comparing `BOOTTIME` with `MONOTONIC` rather than trusted from anywhere.
  Moving the system clock three hours forward changes nothing, and says so in the audit
  log.
- **The policy engine is a pure function.** `(policy, ledger, activity, now) → decision`,
  with no I/O and no clock access, so every rule about a child's day is tested without a
  machine.
- **Enforcement failures lock; convenience failures degrade.** If the daemon dies the
  child's session is locked and the parent is told. If the voice service dies the child
  reads the warning instead of hearing it.

---

## Install

> Not yet. There is no APT repository and no release. This is what installing will look
> like, and it is what `docs/PACKAGING.md` is written against.

On an existing Debian 13 system:

```bash
curl -fsSL https://apt.biokids.bilalgurkansanli.com/KEY.gpg \
  | sudo tee /usr/share/keyrings/biokids-archive-keyring.gpg > /dev/null

echo "deb [signed-by=/usr/share/keyrings/biokids-archive-keyring.gpg] \
https://apt.biokids.bilalgurkansanli.com trixie main" \
  | sudo tee /etc/apt/sources.list.d/biokids.list

sudo apt update && sudo apt install biokids
```

Then open `http://127.0.0.1:7878` and follow the wizard.

A bootable ISO with everything preinstalled will come later.

## Running it from a checkout

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,daemon]"

python dev/seed.py                 # a family, two children, the app catalogue
python -m biokids_daemon           # as root, or --no-logind to develop without one
python -m biokids_panel            # http://127.0.0.1:7878
```

`dev/fakeclock.py` drives the ledger with an injected clock, so a three-hour session
takes three seconds and the whole end-of-time sequence is visible in one screen of
output:

```
         sim     wall     used     left  what  reason             note
 ! + 0:02:10 16:02:10  0:02:00  0:10:00  WARN  quota.warning      speaks warn_10: "On dakikan kaldı."
 ! + 0:11:10 16:11:10  0:11:00  0:01:00  WARN  quota.warning      speaks warn_1: "Bir dakikan kaldı Elif."
!! + 0:12:10 16:12:10  0:12:00  0:00:00  LOCK  quota.exhausted    screen locked, 120 s countdown starts
!! + 0:14:10 16:14:10  0:12:00  0:00:00  END   quota.exhausted    countdown expired -> poweroff
```

`CONTRIBUTING.md` has the VM workflow, which is how anything touching boot, PAM,
nftables or GRUB should be developed.

---

## Documentation

| | |
|---|---|
| [`CLAUDE.md`](CLAUDE.md) | project constitution and conventions |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | components, time engine, policy engine, data model |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | every design decision and why |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | phases and what is deliberately deferred |
| [`docs/SECURITY.md`](docs/SECURITY.md) | threat model, privileges, filtering internals |
| [`docs/AI-LAYER.md`](docs/AI-LAYER.md) | the optional on-device supervision worker |
| [`docs/VOICE.md`](docs/VOICE.md) | Zio and Sio |
| [`docs/UX.md`](docs/UX.md) | design tokens and interface rules |
| [`docs/PACKAGING.md`](docs/PACKAGING.md) | packages, APT repo, ISO, CI |

---

## Contributing

Contributions are welcome. Please read
[`CONTRIBUTING.md`](CONTRIBUTING.md) first — it includes the list of features this
project will **not** accept, such as covert monitoring, keystroke logging, and live
screen mirroring of a child's session.

---

## License

MIT. See [`LICENSE`](LICENSE).

Built under the [BioStudio](https://bilalgurkansanli.com) label by Bilal Gürkan Şanlı.
