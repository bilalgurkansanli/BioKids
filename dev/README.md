# Development tools

Nothing in here ships. These are the scripts that make the difference between
testing BioKids and waiting for BioKids.

| Script | What it is for |
|---|---|
| [`fakeclock.py`](fakeclock.py) | Run a three-hour session in three seconds |
| [`seed.py`](seed.py) | A family, two children and the app catalogue in one command |
| [`reset.sh`](reset.sh) | Drop everything and start again |
| [`check_i18n.py`](check_i18n.py) | Fail when the two catalogues have drifted apart |
| [`check_licenses.py`](check_licenses.py) | Fail when a dependency's licence is not permissive (D-30) |
| [`compile_i18n.py`](compile_i18n.py) | Compile the `.po` files to `.mo`, in pure Python |
| [`make_icons.py`](make_icons.py) | Regenerate the PWA icons |
| [`screenshots.py`](screenshots.py) | Capture the README's screenshots from a running panel |

## The one you will use most

```bash
python dev/fakeclock.py --minutes 65 --quota 60
```

`CLOCK_BOOTTIME` is what makes the time engine impossible to cheat and also what makes
it tedious to watch: a one-hour quota takes an hour to run out. `fakeclock.py` drives
the real ledger and the real policy engine with an injected clock and prints what each
tick decided, so the whole end-of-time sequence — the warnings, the lock, the countdown,
the shutdown — fits on one screen:

```
         sim     wall     used     left  what  reason             note
 ! + 0:02:10 16:02:10  0:02:00  0:10:00  WARN  quota.warning      speaks warn_10: "On dakikan kaldı."
 ! + 0:07:10 16:07:10  0:07:00  0:05:00  WARN  quota.warning      speaks warn_5: "Beş dakikan kaldı."
 ! + 0:11:10 16:11:10  0:11:00  0:01:00  WARN  quota.warning      speaks warn_1: "Bir dakikan kaldı Elif."
!! + 0:12:10 16:12:10  0:12:00  0:00:00  LOCK  quota.exhausted    screen locked, 120 s countdown starts
!! + 0:14:10 16:14:10  0:12:00  0:00:00  END   quota.exhausted    countdown expired -> poweroff
```

The scenario flags are the interesting part: `--suspend`, `--reboot`, `--clock-forward`
and `--clock-back` each reproduce one of the T2.5 cases by hand, which is how you
convince yourself the ledger is right rather than merely green.

It runs against a temporary database under `BIOKIDS_PREFIX` and never touches
`/var/lib/biokids`.

## Running the real thing without root

Every absolute path in `biokids_core.config` is re-rooted by one environment variable:

```bash
export BIOKIDS_PREFIX=/tmp/bk
python dev/seed.py
python -m biokids_panel        # http://127.0.0.1:7878, PIN 284913
python -m biokids_daemon --no-logind
```

`--no-logind` starts the daemon with a session source that sees nobody, so nothing is
enforced. It is for working on the panel and the protocol; it logs a warning saying so,
because a daemon that enforces nothing is exactly what a broken install looks like.

## Reset

```bash
BIOKIDS_PREFIX=/tmp/bk dev/reset.sh
```

Refuses to run without either `BIOKIDS_PREFIX` or an explicit `--system`. You will run
this a hundred times, and one of those times you will be on the wrong machine.

## Screenshots

```bash
python dev/screenshots.py            # the panel, from a running instance
```

Two things it knows that are easy to get wrong: it deletes the target file first, or a
failed capture silently leaves the previous image in place and reports success; and it
takes no phone screenshot, because headless Chromium's `--window-size` sizes the *image*
rather than the viewport, so a narrow capture is a crop of a desktop layout. Phone
screenshots come from a phone, or from devtools emulation.
