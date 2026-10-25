"""The PIN gate, the lockout, and the passphrase gate for destructive actions.

Three secrets, three jobs (D-48):

* the **PIN** is what a parent actually uses on a phone at 22:00, so it is six
  digits and the lockout is what makes that defensible;
* the **passphrase** guards anything that can turn protection off, because a PIN
  a ten-year-old watched being typed must not be enough to uninstall BioKids;
* the **recovery code** is one of the two ways back in (D-50). The other is
  `sudo biokids-admin reset-pin`. There is no email reset and no back door.

The lockout counter lives on the `parent` row rather than in memory, so
restarting the panel — which a child could do by cutting the power — does not
clear it.

A family may have more than one parent (D-218). Each has their own PIN, their
own lockout and their own recovery code, so a child who locks one parent out
has not locked out the other, and the audit log says which of them did what.
The passphrase is the family's: one, whoever is signed in.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from biokids_core import repo, security
from biokids_core.config import Config
from biokids_core.db.models import AuditLog, PanelSession, Parent, utcnow

log = logging.getLogger(__name__)

SESSION_COOKIE = "biokids_session"
PASSPHRASE_GRACE_MINUTES = 5
"""How long one passphrase confirmation covers. Long enough that a single
destructive flow does not ask three times, short enough that a phone left on a
kitchen table is not a standing authorisation."""


@dataclass(frozen=True, slots=True)
class LoginResult:
    ok: bool
    token: str = ""
    error_key: str = ""
    remaining_attempts: int = 0
    lockout_minutes: int = 0
    parent_id: int | None = None
    """Whose PIN this was about, so a refusal can ask the same parent again."""


CHOOSE_KEY = "login.choose"
"""The answer to a PIN that names no parent, or one who cannot sign in: pick
who you are and type it again. No PIN is checked and no counter moves."""


@dataclass(frozen=True, slots=True)
class AuthState:
    """Who is asking, as far as one request is concerned."""

    parent: Parent | None
    session: PanelSession | None

    @property
    def authenticated(self) -> bool:
        return self.parent is not None and self.session is not None

    def passphrase_fresh(self, now: dt.datetime | None = None) -> bool:
        if self.session is None or self.session.passphrase_ok_until is None:
            return False
        return self.session.passphrase_ok_until > (now or utcnow())


# ------------------------------------------------------------------- lockout


def lockout_remaining_minutes(parent: Parent, now: dt.datetime | None = None) -> int:
    if parent.pin_locked_until is None:
        return 0
    now = now or utcnow()
    if parent.pin_locked_until <= now:
        return 0
    return max(1, int((parent.pin_locked_until - now).total_seconds() // 60) + 1)


def _count_failure(session: Session, parent: Parent) -> int:
    """Add one to the failure counter and return *this* request's value.

    Not `parent.pin_failures += 1`. That is a read-modify-write: each concurrent
    request reads the same starting number and writes back the same absolute
    one, so twenty simultaneous guesses cost a single failure. Measured against
    the real app before this change: twenty concurrent wrong PINs left
    `pin_failures = 1` and no lockout at all.

    A plain `UPDATE ... SET x = x + 1` fixes the count but not the decision: the
    threshold is read back through the identity map, where several requests can
    see the same number and cross it together, or miss it entirely. `RETURNING`
    gives each caller the value its own statement produced, so exactly one
    request sees the fifth failure.
    """
    updated = session.execute(
        update(Parent)
        .where(Parent.id == parent.id)
        .values(pin_failures=Parent.pin_failures + 1)
        .returning(Parent.pin_failures)
    ).scalar_one()
    session.refresh(parent, ["pin_failures"])
    return int(updated)


def _next_lockout_minutes(parent: Parent, config: Config) -> int:
    """Doubles on each lockout served, capped. The first is fifteen minutes."""
    base = config.panel.pin_lockout_minutes
    minutes = base * (2 ** int(parent.pin_lockout_level))
    return int(min(minutes, config.panel.pin_lockout_max_minutes))


# --------------------------------------------------------------------- login


def who_signs_in(session: Session, parent_id: int | None) -> Parent | None:
    """The parent a PIN is for.

    With one parent, that parent, whatever the form said: the form an upgrade
    found is the one with no choice on it, and it must go on working (D-218).
    With several, the one the parent picked - and nobody when the pick names no
    one who can sign in, a removed parent included.
    """
    parents = repo.list_parents(session)
    if len(parents) == 1 and parent_id in (None, parents[0].id):
        return parents[0]
    if parent_id is None:
        return None
    return next((parent for parent in parents if parent.id == parent_id), None)


def attempt_login(
    session: Session,
    config: Config,
    pin: str,
    *,
    parent_id: int | None = None,
    user_agent: str = "",
    now: dt.datetime | None = None,
) -> LoginResult:
    """Check one parent's PIN and, on success, mint a session token.

    Every attempt goes to the audit log, successful or not (D-48), under the
    parent it was for. A parent looking at the audit log after a suspicious
    afternoon should be able to see exactly how many times someone tried, and
    at whose PIN.

    The lockout is that parent's alone (D-218): five wrong guesses at Anne's PIN
    do not stop Baba signing in. The names are on the sign-in page for anyone to
    see, so they are not what is protected; the PIN is, and it is counted per
    person.
    """
    now = now or utcnow()
    if not repo.list_parents(session):
        return LoginResult(ok=False, error_key="error.invalid_input")
    parent = who_signs_in(session, parent_id)
    if parent is None:
        return LoginResult(ok=False, error_key=CHOOSE_KEY)

    locked = lockout_remaining_minutes(parent, now)
    if locked:
        repo.audit(
            session,
            actor=f"parent:{parent.id}",
            action="login_locked",
            detail={"minutes_remaining": locked},
            severity="warning",
        )
        return LoginResult(
            ok=False, error_key="login.locked", lockout_minutes=locked, parent_id=parent.id
        )

    if not security.verify_secret(parent.pin_hash, pin.strip()):
        failures = _count_failure(session, parent)
        remaining = max(0, config.panel.pin_attempts - failures)
        detail: dict[str, object] = {"failures": failures}

        if remaining <= 0:
            minutes = _next_lockout_minutes(parent, config)
            parent.pin_locked_until = now + dt.timedelta(minutes=minutes)
            parent.pin_lockout_level += 1
            parent.pin_failures = 0
            detail["lockout_minutes"] = minutes
            repo.audit(
                session,
                actor=f"parent:{parent.id}",
                action="login_locked",
                detail=detail,
                severity="warning",
            )
            return LoginResult(
                ok=False, error_key="login.locked", lockout_minutes=minutes, parent_id=parent.id
            )

        repo.audit(
            session,
            actor=f"parent:{parent.id}",
            action="login_failed",
            detail=detail,
            severity="warning",
        )
        return LoginResult(
            ok=False, error_key="login.wrong", remaining_attempts=remaining, parent_id=parent.id
        )

    parent.pin_failures = 0
    parent.pin_locked_until = None
    parent.pin_lockout_level = 0
    if security.needs_rehash(parent.pin_hash):
        parent.pin_hash = security.hash_secret(pin.strip())

    token = security.generate_token()
    session.add(
        PanelSession(
            token_hash=security.token_fingerprint(token),
            parent_id=parent.id,
            created_at=now,
            last_seen_at=now,
            user_agent=user_agent[:255],
        )
    )
    repo.audit(session, actor=f"parent:{parent.id}", action="login_ok")
    session.flush()
    return LoginResult(ok=True, token=token, parent_id=parent.id)


def resolve(
    session: Session,
    config: Config,
    token: str | None,
    *,
    now: dt.datetime | None = None,
) -> AuthState:
    """Turn a cookie into a parent, expiring the session if it went idle.

    The idle timeout is enforced on read rather than by a background sweep, so
    it is correct even if the panel was not running for the intervening hours.
    """
    if not token:
        return AuthState(parent=None, session=None)
    now = now or utcnow()

    row = session.scalar(
        select(PanelSession).where(PanelSession.token_hash == security.token_fingerprint(token))
    )
    if row is None:
        return AuthState(parent=None, session=None)

    idle_limit = dt.timedelta(minutes=config.panel.session_idle_minutes)
    if now - row.last_seen_at > idle_limit:
        session.delete(row)
        session.flush()
        return AuthState(parent=None, session=None)

    row.last_seen_at = now
    parent = session.get(Parent, row.parent_id)
    if parent is None or parent.removed_at is not None:
        # Removing a parent deletes their sessions (D-218); this is the second
        # lock on the same door, for a row that somehow outlived it.
        session.delete(row)
        session.flush()
        return AuthState(parent=None, session=None)
    return AuthState(parent=parent, session=row)


def logout(session: Session, token: str | None) -> None:
    if not token:
        return
    row = session.scalar(
        select(PanelSession).where(PanelSession.token_hash == security.token_fingerprint(token))
    )
    if row is not None:
        session.delete(row)
        session.flush()


def logout_everywhere(session: Session, parent_id: int) -> int:
    rows = session.scalars(
        select(PanelSession).where(PanelSession.parent_id == parent_id)
    ).all()
    for row in rows:
        session.delete(row)
    session.flush()
    return len(rows)


# ---------------------------------------------------------------- passphrase


PASSPHRASE_ATTEMPTS = 5
PASSPHRASE_WINDOW_MINUTES = 15


def passphrase_locked(session: Session, *, now: dt.datetime | None = None) -> bool:
    """Five wrong passphrases in fifteen minutes stop the gate for the rest of them.

    The passphrase guards switching protection off. Behind a PIN a child may have
    watched being typed, it was the one secret with no limit on guesses. The
    count comes from the audit log, which records every failure already, so
    there is no second counter to fall out of step with it.
    """
    now = now or utcnow()
    since = now - dt.timedelta(minutes=PASSPHRASE_WINDOW_MINUTES)
    # Guesses at a passphrase that has since been changed are not guesses at
    # this one: after `biokids-admin reset-passphrase` (D-220) the new one works
    # at once, not fifteen minutes later.
    changed = session.scalar(
        select(func.max(AuditLog.ts)).where(AuditLog.action == "passphrase_changed")
    )
    if changed is not None and changed > since:
        since = changed
    failures = session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.action == "passphrase_failed", AuditLog.ts >= since)
    )
    return int(failures or 0) >= PASSPHRASE_ATTEMPTS


def confirm_passphrase(
    session: Session,
    state: AuthState,
    passphrase: str,
    *,
    now: dt.datetime | None = None,
) -> bool:
    """Open the grace window for destructive actions, or refuse.

    The passphrase is the family's, whichever parent is signed in (D-218): it
    is the one secret that turns protection off, and a second parent with a
    second passphrase would be a second way to do that. An install with no
    passphrase takes the signed-in parent's own PIN, re-entered, instead: never
    a dead end (principle 4). Either way a failure is audited and counts towards
    `passphrase_locked`.
    """
    if state.parent is None or state.session is None:
        return False
    now = now or utcnow()
    family = repo.family_passphrase_hash(session)
    stored = family or state.parent.pin_hash
    if passphrase_locked(session, now=now) or not security.verify_secret(
        stored, passphrase.strip()
    ):
        repo.audit(
            session,
            actor=f"parent:{state.parent.id}",
            action="passphrase_failed",
            detail={} if family else {"via": "pin_fallback"},
            severity="warning",
        )
        session.flush()
        return False
    state.session.passphrase_ok_until = now + dt.timedelta(minutes=PASSPHRASE_GRACE_MINUTES)
    session.flush()
    return True


# ------------------------------------------------------------------ recovery


def attempt_recovery(
    session: Session,
    code: str,
    new_pin: str,
) -> LoginResult:
    """The eight-word code, then a new PIN. Logged loudly (D-50).

    Each parent has their own code (D-218), and the code says whose it is: it
    is checked against every parent who can sign in, and the PIN that changes
    is the one belonging to the code. Nobody is asked to pick a name first, so
    a code cannot reset somebody else's PIN by being typed under their name.
    Something that is not a code at all is refused before any hash is computed.
    """
    parents = repo.list_parents(session)
    if not parents:
        return LoginResult(ok=False, error_key="error.invalid_input")

    parent = next(
        (p for p in parents if security.verify_recovery_code(p.recovery_hash, code)), None
    )
    if parent is None:
        repo.audit(
            session,
            actor="system",
            action="recovery_failed",
            severity="warning",
        )
        return LoginResult(ok=False, error_key="login.recovery.wrong")

    try:
        pin = security.validate_pin(new_pin)
    except ValueError as exc:
        return LoginResult(ok=False, error_key=str(exc))

    parent.pin_hash = security.hash_secret(pin)
    parent.pin_failures = 0
    parent.pin_locked_until = None
    parent.pin_lockout_level = 0
    logout_everywhere(session, parent.id)
    repo.audit(
        session,
        actor="system",
        action="recovery_used",
        target=f"parent:{parent.id}",
        detail={"note": "the PIN was reset with the eight-word recovery code"},
        severity="alert",
    )
    session.flush()
    return LoginResult(ok=True, parent_id=parent.id)


# ---------------------------------------------------------------------- CSRF


def csrf_token(state: AuthState) -> str:
    """A token derived from the session, so it needs no extra storage.

    The panel is reachable only over localhost or the family's own tailnet, and
    the cookie is `SameSite=Strict`, so this is the third layer rather than the
    first. It costs one hash and closes the gap where a page left open in a
    child's browser could be made to POST.
    """
    if state.session is None:
        return ""
    return security.token_fingerprint(f"csrf:{state.session.token_hash}")[:32]


def csrf_ok(state: AuthState, presented: str | None) -> bool:
    expected = csrf_token(state)
    if not expected:
        return False
    return bool(presented) and security.constant_time_equals(expected, presented or "")


def safe_next(target: str, fallback: str = "/today") -> str:
    r"""Where `?next=` may actually send a parent after signing in.

    `target.startswith("/")` is not enough, and this is the classic way that
    check fails: `//evil.example.com/` starts with a slash, and a browser reads
    a protocol-relative URL and leaves the site. `/\evil.example.com` is treated
    the same way by several browsers, and a backslash survives most naive
    filters. A tab that says "BioKids" in the history and a login form that is
    not ours is exactly the shape of a phishing page for a product whose whole
    job is guarding one PIN.

    So: one leading slash, no second slash or backslash after it, no scheme, no
    control characters. Anything else goes to the fallback rather than being
    repaired - a redirect we do not fully understand is not one to follow.
    """
    if not target or not target.startswith("/"):
        return fallback
    if target.startswith(("//", "/\\")):
        return fallback
    if any(ch in target for ch in "\r\n\t\0") or ":" in target.split("/")[1][:16]:
        return fallback
    return target
