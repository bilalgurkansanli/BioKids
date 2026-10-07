"""Zio and Sio in the parent's own voice (D-174): the lines, recorded here."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Response
from fastapi.responses import HTMLResponse, JSONResponse

from biokids_core import recorded_voice, repo
from biokids_panel.common import View, see_other

router = APIRouter()


@router.get("/voices", response_class=HTMLResponse)
def voices_view(view: View) -> Response:
    """A parent records Zio and Sio's lines in their own voice (D-174)."""
    if blocked := view.guard():
        return blocked
    tr = view.tr
    directory = view.ctx.config.paths.recorded_voice_dir
    locale = tr.locale
    lines = []
    for event in recorded_voice.EVENTS:
        found = recorded_voice.lookup(directory, locale, event)
        lines.append(
            {
                "event": event,
                "recorded": found is not None,
                "text": found.text if found and found.text else tr(f"voices.suggest.{event}"),
                "seconds": round(found.seconds, 1) if found else 0,
            }
        )
    return view.render(
        "voices.html",
        lines=lines,
        locale=locale,
        persona=recorded_voice.persona(directory),
    )


@router.get("/voices/{locale}/{event}.wav")
def voice_file(view: View, locale: str, event: str) -> Response:
    if blocked := view.guard():
        return blocked
    found = recorded_voice.lookup(view.ctx.config.paths.recorded_voice_dir, locale, event)
    if found is None:
        return Response(status_code=404)
    return Response(found.path.read_bytes(), media_type="audio/wav")


@router.post("/voices/{event}")
async def voice_upload(view: View, event: str, text: str = "") -> Response:
    """The recording itself: a WAV body, the CSRF token in a header."""
    tr = view.tr
    if view.guard() is not None:
        return JSONResponse({"ok": False, "error": tr("error.unknown")}, status_code=401)
    if not view.csrf_ok(view.request.headers.get("x-csrf-token")):
        return JSONResponse({"ok": False, "error": tr("error.unknown")}, status_code=403)
    data = await view.request.body()
    try:
        recorded_voice.save(
            view.ctx.config.paths.recorded_voice_dir,
            tr.locale,
            event,
            data,
            text or tr(f"voices.suggest.{event}"),
        )
    except recorded_voice.RecordingError as exc:
        return JSONResponse({"ok": False, "error": tr(str(exc))}, status_code=400)
    except OSError:
        return JSONResponse({"ok": False, "error": tr("error.save_failed")}, status_code=500)
    repo.audit(view.session, actor=view.actor, action="voice_recorded", detail={"event": event})
    view.session.commit()
    return JSONResponse({"ok": True})


@router.post("/voices/{event}/delete")
def voice_delete(view: View, event: str, csrf_token: Annotated[str, Form()] = "") -> Response:
    if blocked := view.guard_form(csrf_token, "/voices?error=csrf"):
        return blocked
    try:
        recorded_voice.delete(view.ctx.config.paths.recorded_voice_dir, view.tr.locale, event)
    except recorded_voice.RecordingError:
        return see_other("/voices?error=1")
    return see_other("/voices?saved=1")


@router.post("/voice-persona")
def voice_persona(
    view: View,
    persona: Annotated[str, Form()] = "zio",
    csrf_token: Annotated[str, Form()] = "",
) -> Response:
    if blocked := view.guard_form(csrf_token, "/voices?error=csrf"):
        return blocked
    try:
        recorded_voice.set_persona(view.ctx.config.paths.recorded_voice_dir, persona)
    except (recorded_voice.RecordingError, OSError):
        return see_other("/voices?error=1")
    return see_other("/voices?saved=1")
