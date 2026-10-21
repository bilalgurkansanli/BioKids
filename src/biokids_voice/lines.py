"""The words Zio and Sio actually say.

VOICE.md §5 is explicit that lines are **data, not code**: one plain-text file
per event per locale, one variant per line, picked at random. A child hears
"ten minutes left" every single afternoon for a year; hearing the same five
words every single afternoon for a year is what makes a companion feel like a
machine. Three phrasings cost three lines of text and buy most of the charm.

Three sources are consulted, in this order:

1. The `voice_line` table, but **only when the caller hands over a session**.
   This service runs as the child user and cannot open the root-owned database
   (`CLAUDE.md` §5), so in practice the panel passes a session — for the line
   editor and the preview — and the service never does. The asymmetry is
   deliberate: the child's session stays away from the family's data entirely.
2. `/usr/share/biokids/voice/<locale>/<event>.txt`, which is what the `.deb`
   installs and what a packager or an administrator may edit.
3. The copy inside `biokids_core/data/voice/`, so a developer running from a
   checkout — and a machine where somebody deleted half of `/usr/share` — both
   still speak.

Parent edits live in the database rather than in the installed file on purpose:
an upgrade must never overwrite a family's own words.
"""

from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

import biokids_core
from biokids_core.config import Config, get_config
from biokids_core.db.models import VoiceLine
from biokids_core.i18n import normalise_locale

LOG = logging.getLogger(__name__)

_CORE_DIR: Final = Path(biokids_core.__file__ or ".").resolve().parent
BUNDLED_LINES_DIR: Final = _CORE_DIR / "data" / "voice"

DEFAULT_EVENTS: Final = (
    "welcome",
    "warn_10",
    "warn_5",
    "warn_1",
    "time_up",
    "window_waiting",
    "bedtime_10",
    "bedtime_5",
    "bedtime_1",
    "bedtime",
    "request_approved",
    "request_denied",
)
"""What Phase 0 ships, and the night's warnings and good night (D-214). VOICE.md
§5 lists more events for later phases; they need no code, only two more text
files each."""

PLACEHOLDERS: Final = ("name", "minutes", "app")
"""The substitutions a line may use. Anything else is dropped rather than shown."""

_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_EVENT_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")
_DOUBLE_SPACE = re.compile(r"[ \t]{2,}")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.!?;:…])")


@dataclass(frozen=True, slots=True)
class SpokenLine:
    """One resolved sentence, ready to be synthesised and shown on the card."""

    event: str
    locale: str
    text: str
    source: str
    """`parent` when a family rewrote it, `package` when it came from a file."""


class LineBook:
    """Every variant of every line, for every locale on this machine."""

    def __init__(
        self,
        config: Config | None = None,
        *,
        rng: random.Random | None = None,
        share_dir: Path | None = None,
    ) -> None:
        self._config = config or get_config()
        # Choosing between three ways of saying "ten minutes left" is not a
        # security decision, and a seedable generator is what makes the choice
        # reproducible in a test.
        self._rng = rng or random.Random()  # noqa: S311
        self._share_dir = share_dir or self._config.paths.voice_lines_dir
        self._cache: dict[tuple[str, str], tuple[str, ...]] = {}

    # ------------------------------------------------------------- reading

    def variants(self, event: str, locale: str) -> tuple[str, ...]:
        """Every phrasing available for one event, installed copy winning."""
        key = (normalise_locale(locale), _safe_event(event))
        if not key[1]:
            return ()
        cached = self._cache.get(key)
        if cached is None:
            cached = self._read(*key)
            self._cache[key] = cached
        return cached

    def events(self, locale: str) -> tuple[str, ...]:
        """Which events this machine can speak, installed and bundled together."""
        locale = normalise_locale(locale)
        found: set[str] = set()
        for directory in self._directories(locale):
            if not directory.is_dir():
                continue
            found |= {p.stem for p in directory.glob("*.txt") if _safe_event(p.stem)}
        return tuple(sorted(found))

    def reload(self) -> None:
        """Forget the parsed files. Called after the panel edits a line."""
        self._cache.clear()

    def _directories(self, locale: str) -> tuple[Path, Path]:
        return (self._share_dir / locale, BUNDLED_LINES_DIR / locale)

    def _read(self, locale: str, event: str) -> tuple[str, ...]:
        for directory in self._directories(locale):
            found = _read_variants(directory / f"{event}.txt")
            if found:
                return found
        return ()

    # ------------------------------------------------------------ choosing

    def render(
        self,
        event: str,
        locale: str,
        variables: dict[str, object] | None = None,
        *,
        session: Session | None = None,
        child_id: int | None = None,
    ) -> SpokenLine | None:
        """Pick a variant and fill it in. None when nothing can be said.

        None is not an error the child should ever see; it means the daemon
        asked for an event this machine has no text for, which the service logs
        and the shell ignores.
        """
        locale = normalise_locale(locale)
        values = _usable(variables or {})

        pool = self._overrides(session, event, locale, child_id) if session else ()
        source = "parent"
        if not pool:
            pool, source = self.variants(event, locale), "package"
        if not pool:
            return None

        return SpokenLine(
            event=event,
            locale=locale,
            text=_substitute(self._choose(pool, values), values),
            source=source,
        )

    def _choose(self, pool: tuple[str, ...], values: dict[str, str]) -> str:
        """Prefer a variant whose every placeholder can actually be filled.

        The daemon does not always know a child's name, and "Hoş geldin, seni
        görmek güzel." is a much better answer to that than a sentence with a
        hole where the name should be. Every event therefore ships at least one
        variant with no placeholders at all, as the last resort.
        """
        complete = [v for v in pool if _placeholders_in(v) <= set(values)]
        return self._rng.choice(complete or list(pool))

    def _overrides(
        self, session: Session, event: str, locale: str, child_id: int | None
    ) -> tuple[str, ...]:
        """A family's own wording, per child first and then for everybody."""
        stmt = select(VoiceLine.text).where(
            VoiceLine.event == event, VoiceLine.locale == locale
        )
        rows: list[str] = []
        if child_id is not None:
            rows = list(session.scalars(stmt.where(VoiceLine.child_id == child_id)).all())
        if not rows:
            rows = list(session.scalars(stmt.where(VoiceLine.child_id.is_(None))).all())
        return tuple(text.strip() for text in rows if text and text.strip())


# ----------------------------------------------------------------- helpers


def _safe_event(event: str) -> str:
    """An event name becomes a filename and arrives over a socket.

    Anything that is not a plain lowercase identifier is refused rather than
    sanitised, because `../../etc/shadow` has no sensible sanitised form.
    """
    candidate = event.strip().lower()
    return candidate if _EVENT_NAME.fullmatch(candidate) else ""


def _read_variants(path: Path) -> tuple[str, ...]:
    """One variant per line. Blank lines and `#` comments are for the reader."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return ()
    found = (line.strip() for line in raw.splitlines())
    return tuple(line for line in found if line and not line.startswith("#"))


def _placeholders_in(text: str) -> set[str]:
    return {match.group(1) for match in _PLACEHOLDER.finditer(text)}


def _usable(variables: dict[str, object]) -> dict[str, str]:
    """Keep the placeholders VOICE.md defines, as strings, dropping empties.

    An empty name is the same as no name: it must not turn a greeting into
    "Merhaba , hoş geldin."
    """
    values: dict[str, str] = {}
    for name in PLACEHOLDERS:
        raw = variables.get(name)
        if raw is None or isinstance(raw, bool):
            continue
        text = str(raw).strip()
        if text:
            values[name] = text
    return values


def _substitute(template: str, values: dict[str, str]) -> str:
    """Fill what we know, remove what we do not, and tidy up after ourselves.

    A parent writing their own line in the panel can reach for a placeholder
    that this event never carries. Leaving `{app}` on screen and in the child's
    ear is worse than a slightly shorter sentence.
    """
    filled = _PLACEHOLDER.sub(lambda m: values.get(m.group(1), ""), template)
    filled = _SPACE_BEFORE_PUNCTUATION.sub(r"\1", filled)
    return _DOUBLE_SPACE.sub(" ", filled).strip()
