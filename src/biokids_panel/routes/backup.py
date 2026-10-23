"""Yedekle ve taşı (D-238): download the family's BioKids, and bring it back.

Ayarlar → Yedekle ve taşı. Taking a backup needs the family's passphrase - the
file holds the children's names, rules and history - and the file is sealed
with that same passphrase, so the new computer asks for it to open it. A
restore makes each child who is not on this computer yet, as adding a child in
the panel does, and fills in what the old computer knew.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Response, UploadFile
from fastapi.responses import HTMLResponse

from biokids_core import backup, repo
from biokids_core.catalog import load_catalog
from biokids_core.db.models import Child, utcnow
from biokids_core.ipc import Op
from biokids_panel import auth
from biokids_panel.common import View, _call_daemon
from biokids_panel.context import AVATARS

log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/settings/backup", response_class=HTMLResponse)
def backup_page(view: View) -> Response:
    if blocked := view.guard():
        return blocked
    return _page(view)


def _page(view: View, *, status_code: int = 200, **extra: Any) -> Response:
    response = view.render(
        "backup.html",
        last=backup.last_taken(view.session),
        has_passphrase=bool(repo.family_passphrase_hash(view.session)),
        children=repo.list_children(view.session),
        **extra,
    )
    response.status_code = status_code
    return response


@router.post("/settings/backup")
def backup_download(
    view: View,
    passphrase: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
) -> Response:
    """The file, for the parent's own browser. Behind the passphrase (D-48)."""
    if blocked := view.guard_form(csrf_token, "/settings/backup?error=csrf"):
        return blocked
    session = view.session
    if not repo.family_passphrase_hash(session):
        return _page(view, status_code=400, error=view.tr("backup.error.no_passphrase"))
    if not auth.confirm_passphrase(session, view.state, passphrase):
        session.commit()
        return _page(view, status_code=403, error=view.tr("backup.error.passphrase"))
    contents = backup.export(session, recordings_dir=view.ctx.config.paths.recorded_voice_dir)
    data = backup.seal(contents, passphrase)
    repo.set_setting(
        session, backup.LAST_BACKUP_SETTING, utcnow().isoformat(timespec="seconds")
    )
    repo.audit(
        session,
        actor=view.actor,
        action="backup_taken",
        detail={"children": len(contents["children"])},
        severity="warning",
    )
    session.commit()
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{backup.file_name()}"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/settings/backup/restore")
def backup_restore(
    view: View,
    file: Annotated[UploadFile, File()],
    passphrase: Annotated[str, Form()] = "",
    old_passphrase: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
) -> Response:
    """The other half: the old computer's file, behind this computer's passphrase.

    A restore can change every child's rules, so it is behind this machine's own
    family passphrase (D-48), like turning protection off; the file is opened
    with the old machine's, the same one when the parent kept it. A plain
    `def`: the key takes a second to make and the provisioning runs systemctl,
    neither of which may hold up the panel's other requests.
    """
    if blocked := view.guard_form(csrf_token, "/settings/backup?error=csrf"):
        return blocked
    session = view.session
    if not auth.confirm_passphrase(session, view.state, passphrase):
        session.commit()
        return _page(view, status_code=403, error=view.tr("backup.error.this_passphrase"))
    data = file.file.read(backup.MAX_FILE_BYTES + 1)
    try:
        contents = backup.open_sealed(data, old_passphrase.strip() or passphrase)
    except backup.BackupError as exc:
        repo.audit(
            session,
            actor=view.actor,
            action="backup_restore_failed",
            detail={"why": exc.key},
            severity="warning",
        )
        session.commit()
        return _page(view, status_code=400, error=view.tr(exc.key))

    from biokids_panel.wizard import create_child, provision_child

    def make(session_: Any, entry: Any) -> Child | None:
        fields = entry.get("child") or {}
        year = fields.get("birth_year")
        child, _error = create_child(
            session_,
            display_name=str(fields.get("display_name", "")),
            birth_year=year if isinstance(year, int) else dt.date.today().year - 6,
            unix_user=str(entry.get("unix_user", "")),
            avatar=str(fields.get("avatar", "fox")),
            locale=str(fields.get("locale", "tr")),
        )
        return child

    known = {entry.slug for entry in load_catalog(config=view.ctx.config).entries}
    done = backup.restore(
        session,
        contents,
        create_child=make,
        known_apps=known,
        known_avatars={slug for slug, _glyph in AVATARS},
        recordings_dir=view.ctx.config.paths.recorded_voice_dir,
    )
    repo.audit(
        session,
        actor=view.actor,
        action="backup_restored",
        detail={"children": len(done.children), "new": len(done.created)},
        severity="warning",
    )
    session.commit()
    # A child who had no account here gets one, as adding them in the panel
    # does; never fatal, and the audit log says which it was.
    for child in done.created:
        provision_child(session, child)
    session.commit()
    _call_daemon(view.ctx, Op.RELOAD)
    return _page(view, restored=done)
