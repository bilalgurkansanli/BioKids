"""Ayarlar → Ebeveynler: who may sign in to the panel (D-218)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Form, Response
from fastapi.responses import HTMLResponse

from biokids_core import repo, security
from biokids_panel import auth, parents
from biokids_panel.common import PanelView, View, see_other

router = APIRouter()

PAGE = "/settings/parents"


@router.get(PAGE, response_class=HTMLResponse)
def parents_view(view: View) -> Response:
    if blocked := view.guard():
        return blocked
    return _page(view)


@router.post(f"{PAGE}/add")
def parent_add(
    view: View,
    name: Annotated[str, Form()] = "",
    pin: Annotated[str, Form()] = "",
    pin_again: Annotated[str, Form()] = "",
    recovery: Annotated[str, Form()] = "",
    written_down: Annotated[str, Form()] = "",
    passphrase: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
) -> Response:
    """A new parent with their own PIN and recovery code, behind the passphrase.

    The gate comes first and the form's own checks second, as for removing a
    child: a wrong passphrase says nothing about the rest, and a typo in the
    PIN afterwards is fixed within the five-minute grace without typing the
    passphrase again.
    """
    if blocked := view.guard_form(csrf_token, f"{PAGE}?error=csrf"):
        return blocked
    session, state = view.session, view.state
    # What the form keeps when it comes back with a reason: the name and the
    # code the new parent may already have written down; never a PIN.
    kept: dict[str, Any] = {
        "name": parents.clean_name(name),
        "recovery": recovery,
        "adding": True,
    }
    if passphrase:
        auth.confirm_passphrase(session, state, passphrase)
    if not state.passphrase_fresh():
        session.commit()
        return _page(view, status_code=403, error=view.tr("auth.passphrase.wrong"), **kept)

    parent, error = parents.add_parent(
        session,
        name=name,
        pin=pin,
        pin_again=pin_again,
        recovery=recovery,
        written_down=written_down == "on",
        actor=view.actor,
    )
    if parent is None:
        session.commit()
        if error == "wizard.recovery.replaced":
            # Not the code the page showed. A fresh one, with the reason,
            # rather than the hash of whatever arrived (as in the wizard).
            kept["recovery"] = security.generate_recovery_code()
        return _page(view, status_code=400, error=view.tr(error), **kept)
    session.commit()
    return see_other(f"{PAGE}?saved=parent_added")


@router.post(PAGE + "/{parent_id}/remove")
def parent_remove(
    view: View,
    parent_id: int,
    passphrase: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
) -> Response:
    if blocked := view.guard_form(csrf_token, f"{PAGE}?error=csrf"):
        return blocked
    session, state = view.session, view.state
    target = repo.get_active_parent(session, parent_id)
    if target is None or state.parent is None:
        return see_other(f"{PAGE}?error=error.not_found")
    if passphrase:
        auth.confirm_passphrase(session, state, passphrase)
    if not state.passphrase_fresh():
        session.commit()
        return see_other(f"{PAGE}?error=passphrase")
    if error := parents.removal_error(session, target, signed_in=state.parent):
        return see_other(f"{PAGE}?error={error}")

    yourself = target.id == state.parent.id
    parents.remove_parent(session, target, actor=view.actor)
    session.commit()
    if yourself:
        # Their sessions went with them, this one included.
        response = see_other("/login")
        response.delete_cookie(auth.SESSION_COOKIE, path="/")
        return response
    return see_other(f"{PAGE}?saved=parent_removed")


@router.post(f"{PAGE}/name")
def parent_rename(
    view: View,
    name: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
) -> Response:
    """The signed-in parent's own name; nobody renames somebody else."""
    if blocked := view.guard_form(csrf_token, f"{PAGE}?error=csrf"):
        return blocked
    parent = view.state.parent
    if parent is None:
        return see_other(f"{PAGE}?error=csrf")
    if error := parents.rename(view.session, parent, name, actor=view.actor):
        return see_other(f"{PAGE}?error={error}")
    view.session.commit()
    return see_other(f"{PAGE}?saved=1")


def _page(view: PanelView, status_code: int = 200, **values: Any) -> HTMLResponse:
    session, state = view.session, view.state
    everyone = repo.list_parents(session)
    values.setdefault("name", "")
    values.setdefault("adding", False)
    if not security.is_recovery_code(str(values.get("recovery", ""))):
        # Generated for this render and never stored in plaintext: only the
        # hash of the one posted back survives (D-50).
        values["recovery"] = security.generate_recovery_code()
    me = state.parent
    return view.render(
        "parents.html",
        status_code=status_code,
        parents=parents.shown(everyone, view.tr, you=me.id if me else None),
        me=me,
        can_add=len(everyone) < parents.MAX_PARENTS,
        max_parents=parents.MAX_PARENTS,
        passphrase_fresh=state.passphrase_fresh(),
        no_passphrase=not repo.family_passphrase_hash(session),
        **values,
    )
