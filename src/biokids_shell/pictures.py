"""Resimlerim: the pictures a child made, on their own launcher (D-212).

Tux Paint ("Boya") saves into `~/.tuxpaint/saved/`, and its thumbnails into a
folder inside it; KolourPaint ("Çizim") and the rest save where the child's
Pictures folder is - `xdg-user-dir PICTURES`, `~/Resimler` on a Turkish
account, `~/Pictures` on an English one. The launcher runs as the child (a user
unit), so it can read these as the child can, and shows them in a grid with a
viewer. Nothing is sent anywhere, and the parent's panel never gets them: they
are the child's, like a drawer of drawings.

What this module guarantees, because the launcher serves files to a browser:

- Only from those folders, and only their own top level: not the thumbnails'
  folder, not a hidden file, not a folder inside, never the home folder itself
  (a Pictures folder set to `$HOME` is skipped).
- A folder may be a symbolic link only to somewhere inside the child's home,
  and a picture never is one: a link to another account's file is refused
  however it is named.
- Only what belongs to the child: every file is checked to be owned by the uid
  the launcher runs as, on the open file itself, so nothing swapped in between
  the check and the read is served.
- Only pictures a browser shows as pictures: PNG, JPEG, GIF, WebP and BMP, by
  their name *and* their first bytes. Never SVG, which can carry a script.
- A picture is named by its folder's key and its file name, and the name must
  be a plain name: no separators, no `..`, nothing hidden.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

IMAGE_TYPES: Final[Mapping[str, str]] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}

_MAGIC: Final[Mapping[str, tuple[bytes, ...]]] = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "image/bmp": (b"BM",),
}

MAX_BYTES: Final = 32 * 1024 * 1024
"""A drawing is a few hundred kilobytes; a file this big is not one, and the
launcher would hold it in memory to serve it."""

MAX_SHOWN: Final = 500
"""The newest this many. A child with more has a drawer full; the grid stays fast."""

_NAME: Final = re.compile(r"[^/\\\x00]{1,255}")


@dataclass(frozen=True, slots=True)
class Folder:
    key: str
    """Stable in a URL: `tuxpaint`, `xdg`, `resimler` or `pictures`."""
    path: Path
    """Resolved: a link already followed, and found inside the home folder."""


@dataclass(frozen=True, slots=True)
class Picture:
    folder: str
    name: str
    path: Path
    mtime: float
    media_type: str

    @property
    def day(self) -> dt.date:
        """When it was last saved, on this machine's calendar."""
        return dt.datetime.fromtimestamp(self.mtime).date()


def _own_uid() -> int | None:
    """The uid the launcher runs as; None on a system without uids (the
    Windows host the tests also run on), where ownership is not checked."""
    getuid = getattr(os, "getuid", None)
    return int(getuid()) if getuid is not None else None


def xdg_pictures_dir(home: Path, environ: Mapping[str, str] | None = None) -> Path | None:
    """`xdg-user-dir PICTURES`, read from `user-dirs.dirs` without running it.

    The file holds lines such as `XDG_PICTURES_DIR="$HOME/Resimler"`. Only a
    path under `$HOME`, or an absolute one, is taken; whatever else is there
    is ignored, as xdg-user-dir does.
    """
    environ = os.environ if environ is None else environ
    config_home = Path(environ.get("XDG_CONFIG_HOME") or home / ".config")
    try:
        text = (config_home / "user-dirs.dirs").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    for line in text.splitlines():
        match = re.fullmatch(r'\s*XDG_PICTURES_DIR\s*=\s*"([^"]*)"\s*', line)
        if match is None:
            continue
        value = match.group(1)
        if value.startswith("$HOME"):
            return home / value.removeprefix("$HOME").lstrip("/")
        if value.startswith("/"):
            return Path(value)
    return None


def folders(home: Path, environ: Mapping[str, str] | None = None) -> list[Folder]:
    """The folders a child's pictures are in, each at most once."""
    candidates: list[tuple[str, Path | None]] = [
        ("tuxpaint", home / ".tuxpaint" / "saved"),
        ("xdg", xdg_pictures_dir(home, environ)),
        ("resimler", home / "Resimler"),
        ("pictures", home / "Pictures"),
    ]
    try:
        own_home = home.resolve(strict=True)
    except OSError:
        return []
    uid = _own_uid()
    found: list[Folder] = []
    seen: set[Path] = set()
    for key, path in candidates:
        if path is None:
            continue
        try:
            resolved = path.resolve(strict=True)
            info = resolved.stat()
        except (OSError, RuntimeError):
            continue
        if not stat.S_ISDIR(info.st_mode) or resolved in seen:
            continue
        if resolved == own_home or not resolved.is_relative_to(own_home):
            # Pictures set to the home folder itself would list everything in
            # it; and a link out of the home is another account's business.
            continue
        if uid is not None and info.st_uid != uid:
            continue
        seen.add(resolved)
        found.append(Folder(key, resolved))
    return found


def media_type(name: str) -> str | None:
    """The type a file's name claims, or None for anything not a picture."""
    return IMAGE_TYPES.get(Path(name).suffix.lower())


def plain_name(name: str) -> bool:
    """A file name and nothing more: no folder, no `..`, nothing hidden."""
    return bool(_NAME.fullmatch(name)) and not name.startswith(".")


def looks_like(head: bytes, kind: str) -> bool:
    """Whether a file's first bytes are the picture its name says."""
    if kind == "image/webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    return any(head.startswith(magic) for magic in _MAGIC.get(kind, ()))


def pictures(home: Path, environ: Mapping[str, str] | None = None) -> list[Picture]:
    """Every picture in the child's folders, newest first, at most `MAX_SHOWN`."""
    uid = _own_uid()
    found: list[Picture] = []
    for folder in folders(home, environ):
        try:
            entries = list(os.scandir(folder.path))
        except OSError:
            continue
        for entry in entries:
            kind = media_type(entry.name)
            if kind is None or not plain_name(entry.name):
                continue
            try:
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    continue
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if (uid is not None and info.st_uid != uid) or not 0 < info.st_size <= MAX_BYTES:
                continue
            found.append(Picture(folder.key, entry.name, Path(entry.path), info.st_mtime, kind))
    found.sort(key=lambda p: (-p.mtime, p.folder, p.name))
    return [p for p in found[: MAX_SHOWN * 2] if _head_matches(p)][:MAX_SHOWN]


def _head_matches(picture: Picture) -> bool:
    try:
        with picture.path.open("rb") as handle:
            return looks_like(handle.read(16), picture.media_type)
    except OSError:
        return False


def find(
    home: Path, folder: str, name: str, environ: Mapping[str, str] | None = None
) -> Picture | None:
    """One picture by its folder's key and its name, or None.

    "Or None" is the point, as with a tile (app.py `_find_tile`): anything
    that is not plainly a picture of the child's in one of their folders does
    not resolve to anything.
    """
    kind = media_type(name)
    if kind is None or not plain_name(name):
        return None
    place = next((f for f in folders(home, environ) if f.key == folder), None)
    if place is None:
        return None
    path = place.path / name
    try:
        info = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or path.parent != place.path:
        return None
    return Picture(place.key, name, path, info.st_mtime, kind)


def read(picture: Picture) -> bytes | None:
    """The picture's bytes, checked on the open file itself, or None.

    Opened without following a link, then checked through the descriptor:
    a regular file, the child's own, no bigger than `MAX_BYTES`, and starting
    the way its kind of picture starts. What is read is what was checked.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(picture.path, flags)
    except OSError:
        return None
    with contextlib.closing(os.fdopen(descriptor, "rb")) as handle:
        try:
            info = os.fstat(handle.fileno())
            uid = _own_uid()
            if not stat.S_ISREG(info.st_mode) or (uid is not None and info.st_uid != uid):
                return None
            if not 0 < info.st_size <= MAX_BYTES:
                return None
            data = handle.read(MAX_BYTES + 1)
        except OSError:
            return None
    if len(data) > MAX_BYTES or not looks_like(data[:16], picture.media_type):
        return None
    return data


def neighbours(
    shown: list[Picture], folder: str, name: str
) -> tuple[int, Picture | None, Picture | None] | None:
    """Where a picture is in the grid's order, and the ones either side of it."""
    for index, picture in enumerate(shown):
        if picture.folder == folder and picture.name == name:
            newer = shown[index - 1] if index > 0 else None
            older = shown[index + 1] if index + 1 < len(shown) else None
            return index, newer, older
    return None


__all__ = [
    "IMAGE_TYPES",
    "MAX_BYTES",
    "MAX_SHOWN",
    "Folder",
    "Picture",
    "find",
    "folders",
    "looks_like",
    "media_type",
    "neighbours",
    "pictures",
    "plain_name",
    "read",
    "xdg_pictures_dir",
]
