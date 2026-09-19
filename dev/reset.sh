#!/bin/sh
# Throw away a BioKids installation's data and start again.
#
# This is the script you will run a hundred times while building the wizard, and
# one of those hundred times you will be on the wrong machine. So it refuses to
# do anything unless you have said which machine you mean: BIOKIDS_PREFIX for a
# scratch tree on a development host, or an explicit --system for the real paths
# under /var. It prints every path it is about to delete before deleting it, and
# --system additionally asks out loud.
#
# It does not touch /etc/biokids: the configuration and the secret key belong to
# the machine, not to the family, and losing the key means losing every stored
# SMTP password with it.
#
# Deleting the database deletes the schema with it, and nothing in the daemon or
# the panel creates one — that has always been `alembic upgrade head` from
# biokids-core.postinst. So --system runs the same migration before starting
# anything, and refuses to start the units if it cannot: a daemon restarted
# against a database with no tables crash-loops, and a crash-looping daemon
# pulls in biokids-lockdown.service and locks every child session.
#
# POSIX sh on purpose — this runs on a bare Debian install where /bin/sh is dash.

set -eu

PROGRAM="$(basename "$0")"
SYSTEM=0
ASSUME_YES=0

die() {
    printf '%s: %s\n' "$PROGRAM" "$1" >&2
    exit "${2:-1}"
}

usage() {
    cat <<EOF
Usage: $PROGRAM [--system] [--yes]

Deletes the BioKids database, data directory, logs and runtime socket, recreates
the directories a fresh install would have, and with --system recreates the
schema with alembic before restarting the units.

  --system   operate on the real /var paths and restart systemd units (needs root)
  --yes      skip the confirmation prompt that --system otherwise requires
  --help     show this

Without --system, BIOKIDS_PREFIX must be set and everything happens inside it;
there the schema is left to dev/seed.py, which creates it directly.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --system) SYSTEM=1 ;;
        --yes|-y) ASSUME_YES=1 ;;
        --help|-h) usage; exit 0 ;;
        *) usage >&2; die "unknown option: $1" 2 ;;
    esac
    shift
done

PREFIX="${BIOKIDS_PREFIX:-}"

if [ "$SYSTEM" -eq 1 ] && [ -n "$PREFIX" ]; then
    die "BIOKIDS_PREFIX is set and --system was given; unset one so it is clear which tree you mean" 2
fi
if [ "$SYSTEM" -eq 0 ] && [ -z "$PREFIX" ]; then
    die "set BIOKIDS_PREFIX to a scratch directory, or pass --system to reset the real installation" 2
fi
if [ "$SYSTEM" -eq 1 ] && [ "$(id -u)" -ne 0 ]; then
    die "--system needs root: try sudo sh $0 --system" 2
fi

# Every guard below compares a path as text, so the prefix is resolved first.
# BIOKIDS_PREFIX=/ would otherwise build "//var/lib/biokids" — the real
# installation, spelled in a way no guard recognises — and delete it with
# neither the root check nor the confirmation that --system insists on. The
# same goes for "/." and for a symlink that happens to point at the root.
if [ -n "$PREFIX" ]; then
    if [ -d "$PREFIX" ]; then
        PREFIX="$(cd "$PREFIX" && pwd -P)"
    fi
    case "$PREFIX" in
        /|//)
            die "BIOKIDS_PREFIX resolves to the root directory, which is the real installation; only --system may reset that" 2
            ;;
    esac
fi

DATA_DIR="$PREFIX/var/lib/biokids"
EVIDENCE_DIR="$PREFIX/var/lib/biokids/evidence"
LOG_DIR="$PREFIX/var/log/biokids"
RUN_DIR="$PREFIX/run/biokids"
ALEMBIC_INI="$PREFIX/usr/share/biokids/alembic.ini"
UNITS="biokids-daemon.service biokids-panel.service"

# A typo in BIOKIDS_PREFIX must not turn into `rm -rf /var/lib`. Every target is
# checked against the directories that are only ever a parent, never a target.
guard_path() {
    case "$1" in
        ""|/|/var|/var/lib|/var/log|/run|/usr|/etc|/home|/root)
            die "refusing to delete $1"
            ;;
    esac
}

describe() {
    target="$1"
    if [ -d "$target" ]; then
        count="$(find "$target" -type f 2>/dev/null | wc -l | tr -d ' ')"
        printf '  delete  %s  (%s file(s))\n' "$target" "$count"
    elif [ -e "$target" ]; then
        printf '  delete  %s\n' "$target"
    else
        printf '  absent  %s\n' "$target"
    fi
}

unit_exists() {
    command -v systemctl >/dev/null 2>&1 && systemctl cat "$1" >/dev/null 2>&1
}

for target in "$DATA_DIR" "$LOG_DIR" "$RUN_DIR"; do
    guard_path "$target"
done

if [ "$SYSTEM" -eq 1 ]; then
    printf 'BioKids reset — SYSTEM installation\n'
else
    printf 'BioKids reset — BIOKIDS_PREFIX=%s\n' "$PREFIX"
fi
describe "$DATA_DIR"
describe "$LOG_DIR"
describe "$RUN_DIR"
printf '  keep    %s/etc/biokids  (config and secret key)\n' "$PREFIX"

if [ "$SYSTEM" -eq 1 ] && [ "$ASSUME_YES" -eq 0 ]; then
    printf 'This deletes every family, policy and usage record. Type "yes" to continue: '
    # An empty answer on EOF is the safe one: a reset that runs unattended from a
    # pipe is exactly the accident this prompt exists to prevent.
    read -r reply || reply=""
    [ "$reply" = "yes" ] || die "aborted; nothing was deleted"
fi

INSTALLED=0
if [ "$SYSTEM" -eq 1 ]; then
    for unit in $UNITS; do
        if unit_exists "$unit"; then
            INSTALLED=1
            printf '  stop    %s\n' "$unit"
            # Not `|| true`: a daemon that refused to stop is a daemon still
            # writing to the file we are about to delete, and carrying on would
            # leave it running against a database that no longer exists.
            systemctl stop "$unit" \
                || die "could not stop $unit; nothing was deleted"
        else
            printf '  skip    %s (not installed)\n' "$unit"
        fi
    done
else
    printf '  units   not touched (no systemd units in a prefix tree)\n'
fi

rm -rf -- "$DATA_DIR" "$LOG_DIR" "$RUN_DIR"

# Recreated straight away, with the modes biokids-core.postinst asserts, so that
# a reset tree and a freshly installed one are the same tree. The daemon runs as
# root, but /var/lib/biokids holds a child's usage history and never becomes
# world-readable; the log directory is group-readable so an admin can read it.
mkdir -p -- "$DATA_DIR" "$EVIDENCE_DIR" "$LOG_DIR" "$RUN_DIR"
chmod 700 "$DATA_DIR" "$EVIDENCE_DIR"
chmod 750 "$LOG_DIR"
chmod 755 "$RUN_DIR"
if [ "$SYSTEM" -eq 1 ] && getent group adm >/dev/null 2>&1; then
    chgrp adm "$LOG_DIR"
fi

# The schema, and only then the units. Deleting the database deleted every table
# with it, and neither the daemon nor the panel creates one — that has always
# been Alembic, from postinst. Starting a daemon against a database with no
# tables gives a crash loop, and a crash-looping daemon pulls in
# biokids-lockdown.service, so the schema failing is a reason to leave the units
# stopped and say so, not a reason to start them anyway.
if [ "$SYSTEM" -eq 1 ] && [ "$INSTALLED" -eq 1 ]; then
    if [ ! -f "$ALEMBIC_INI" ] || ! command -v alembic >/dev/null 2>&1; then
        die "no schema was created ($ALEMBIC_INI is missing, or alembic is not installed) and the units are left stopped; create it, then: systemctl start $UNITS"
    fi
    printf '  schema  alembic -c %s upgrade head\n' "$ALEMBIC_INI"
    alembic -c "$ALEMBIC_INI" upgrade head >/dev/null \
        || die "alembic upgrade head failed; the units are left stopped rather than started against a database with no tables"

    for unit in $UNITS; do
        if unit_exists "$unit"; then
            printf '  start   %s\n' "$unit"
            systemctl start "$unit"
        fi
    done
fi

printf 'done. Next: dev/seed.py writes a family you can log in as.\n'
