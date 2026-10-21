"""More than one parent (D-218): adding one, removing one, and naming them.

The panel began with one parent account, and a family has two parents more
often than one. Each parent added here has their own name, their own PIN with
its own lockout, and their own recovery code, shown once; what they do is
audited under their own name. What the family shares stays shared: the
passphrase that turns protection off, the mail settings, the children's
requests - any parent may answer them.

Adding and removing a parent both sit behind the passphrase gate, because a new
PIN that opens the panel is exactly what a child who has watched one PIN being
typed would want to add. Nothing here removes the last parent, or removes the
signed-in parent when nobody else could sign in afterwards (principle 4).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final

from sqlalchemy.orm import Session

from biokids_core import repo, security
from biokids_core.db.models import Parent, utcnow
from biokids_core.i18n import Translator
from biokids_panel import auth

log = logging.getLogger(__name__)

MAX_PARENTS: Final = 4
"""Enough for two parents and the grandparents who collect from school; few
enough that the sign-in page is a handful of big buttons, not a list."""

NAME_LENGTH: Final = 64


@dataclass(frozen=True, slots=True)
class Shown:
    """A parent as the sign-in page and the Ebeveynler screen show them."""

    id: int
    name: str
    you: bool = False


def shown(parents: list[Parent], tr: Translator, *, you: int | None = None) -> list[Shown]:
    """Each parent with a name to show.

    The wizard never insisted on a name, so the first parent may have none: they
    are "Ebeveyn 1" until they give one. Two buttons that both say nothing is
    the sign-in page this prevents.
    """
    return [
        Shown(
            id=parent.id,
            name=parent.display_name or tr("parents.unnamed", number=position),
            you=parent.id == you,
        )
        for position, parent in enumerate(parents, start=1)
    ]


def clean_name(name: str) -> str:
    return " ".join(name.split())[:NAME_LENGTH]


def _name_error(session: Session, name: str, *, besides: int | None = None) -> str:
    if not name:
        return "parents.name.missing"
    taken = repo.find_parent_by_name(session, name)
    if taken is not None and taken.id != besides:
        # Two "Anne" buttons on the sign-in page, and a five-wrong-guesses
        # lockout on whichever one a tired parent pressed.
        return "parents.name.taken"
    return ""


def add_parent(
    session: Session,
    *,
    name: str,
    pin: str,
    pin_again: str,
    recovery: str,
    written_down: bool,
    actor: str,
) -> tuple[Parent | None, str]:
    """Create a parent who can sign in, or say why not (an i18n key).

    The recovery code is the one the form showed, sent back, as in the wizard
    (D-50): checked to be eight words from the list, then only its hash is kept.
    The caller has already passed the passphrase gate.
    """
    if len(repo.list_parents(session)) >= MAX_PARENTS:
        return None, "parents.full"
    chosen_name = clean_name(name)
    if error := _name_error(session, chosen_name):
        return None, error
    if pin.strip() != pin_again.strip():
        return None, "wizard.parent.pin.mismatch"
    try:
        chosen_pin = security.validate_pin(pin)
    except ValueError as exc:
        return None, str(exc)
    if not security.is_recovery_code(recovery):
        return None, "wizard.recovery.replaced"
    if not written_down:
        return None, "parents.recovery.not_confirmed"

    parent = Parent(
        display_name=chosen_name,
        pin_hash=security.hash_secret(chosen_pin),
        recovery_hash=security.hash_secret(security.normalise_recovery_code(recovery)),
    )
    session.add(parent)
    session.flush()
    # A warning, and mailed with the alerts (biokids_daemon.notifier): a new
    # PIN that opens the panel is the change a family most needs to hear about.
    repo.audit(
        session,
        actor=actor,
        action="parent_added",
        target=f"parent:{parent.id}",
        detail={"name": parent.display_name},
        severity="warning",
    )
    return parent, ""


def removal_error(session: Session, parent: Parent, *, signed_in: Parent) -> str:
    """Why this parent may not be removed now, or "" when they may.

    Never the last parent: nobody could sign in. Never the signed-in parent
    unless somebody else still can - a parent with a PIN to type.
    """
    if parent.removed_at is not None:
        return "error.not_found"
    others = [p for p in repo.list_parents(session) if p.id != parent.id]
    if not others:
        return "parents.remove.last"
    if parent.id == signed_in.id and not any(p.pin_hash for p in others):
        return "parents.remove.last"
    return ""


def remove_parent(session: Session, parent: Parent, *, actor: str) -> None:
    """Stop a parent signing in, keeping their name for the audit log.

    Their PIN, recovery code and sessions go at once. If they held what the
    family shares - the passphrase and the mail settings, on the first parent
    still in use - it moves to the parent who now comes first, so removing the
    parent who ran the wizard does not quietly remove the gate on turning
    protection off.
    """
    holder = repo.get_parent(session)
    successor = next((p for p in repo.list_parents(session) if p.id != parent.id), None)
    if holder is not None and holder.id == parent.id and successor is not None:
        if not successor.passphrase_hash:
            successor.passphrase_hash = parent.passphrase_hash
        if successor.smtp_json_encrypted is None:
            successor.smtp_json_encrypted = parent.smtp_json_encrypted
            successor.email = successor.email or parent.email

    parent.removed_at = utcnow()
    parent.pin_hash = ""
    parent.passphrase_hash = ""
    parent.recovery_hash = ""
    parent.smtp_json_encrypted = None
    parent.email = None
    parent.pin_failures = 0
    parent.pin_locked_until = None
    parent.pin_lockout_level = 0
    auth.logout_everywhere(session, parent.id)
    repo.audit(
        session,
        actor=actor,
        action="parent_removed",
        target=f"parent:{parent.id}",
        detail={"name": parent.display_name},
        severity="warning",
    )
    session.flush()


def rename(session: Session, parent: Parent, name: str, *, actor: str) -> str:
    """A parent's own name, as the sign-in page shows it. "" when it was saved."""
    chosen = clean_name(name)
    if error := _name_error(session, chosen, besides=parent.id):
        return error
    if chosen == parent.display_name:
        return ""
    before = parent.display_name
    parent.display_name = chosen
    repo.audit(
        session,
        actor=actor,
        action="parent_renamed",
        target=f"parent:{parent.id}",
        detail={"before": before, "after": chosen},
    )
    return ""
