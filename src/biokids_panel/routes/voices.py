"""Zio and Sio's voice: the natural one a parent downloads (D-196), and the
parent's own, recorded here (D-174)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Form, Response
from fastapi.responses import HTMLResponse, JSONResponse

from biokids_core import natural_voice, recorded_voice, repo
from biokids_core.ipc import Op
from biokids_panel.common import PanelView, View, _call_daemon, see_other

router = APIRouter()


def _natural(view: PanelView) -> dict[str, Any]:
    """What the natural-voice card and its page show (D-196)."""
    config = view.ctx.config
    state = natural_voice.state(config)
    return {
        "state": state,
        "speaks": natural_voice.natural_speaks(config),
        # The English corner's voice (D-225), which a family that downloaded the
        # Turkish one before it existed does not have yet.
        "english": natural_voice.english_speaks(config),
        "english_mb": round(natural_voice.ENGLISH_BYTES / 1_000_000),
        # A failed download the daemon will start again by itself (D-223).
        "retrying": state in {natural_voice.FAILED, natural_voice.ABSENT}
        and repo.get_setting(view.session, natural_voice.WANTED_SETTING, "")
        == natural_voice.WANTED,
        "size_mb": round(natural_voice.DOWNLOAD_BYTES / 1_000_000),
        "progress": natural_voice.progress(config) if state == natural_voice.RUNNING else 0,
    }


@router.get("/voices", response_class=HTMLResponse)
def voices_view(view: View) -> Response:
    """A parent records Zio and Sio's lines in their own voice (D-174), and can
    give them a natural voice for every other line (D-196)."""
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
        natural=_natural(view),
    )


@router.get("/voice-natural", response_class=HTMLResponse)
def natural_view(view: View, started: str = "") -> Response:
    """The download, followed on a page of its own that refreshes itself.

    Not on /voices: that page has a sentence field for every line and may be
    halfway through a recording, and a refresh would lose both.
    """
    if blocked := view.guard():
        return blocked
    return view.render("voice_natural.html", natural=_natural(view), started=started == "1")


@router.post("/voice-natural")
def natural_download(view: View, csrf_token: Annotated[str, Form()] = "") -> Response:
    """Start the one-time download (D-196), run by the daemon as its own unit."""
    if blocked := view.guard_form(csrf_token, "/voices?error=csrf"):
        return blocked
    repo.audit(view.session, actor=view.actor, action="voice_download")
    # Asked for, so tried again until it is there (D-223).
    repo.set_setting(view.session, natural_voice.WANTED_SETTING, natural_voice.WANTED)
    view.session.commit()
    if not _call_daemon(view.ctx, Op.VOICE_SETUP):
        return see_other("/voices?error=voices.natural.not_started")
    return see_other("/voice-natural?started=1")


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
