#!/usr/bin/env python3
"""Fail if the translation catalogs have drifted apart.

Every message id present in one locale must be present in all of them, and no
catalog may carry an empty translation. This is the guard behind T1.3: a missing
Turkish string is a bug the same way a missing function is.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCALE_DIR = ROOT / "src" / "biokids_core" / "locale"


def parse_po(path: Path) -> dict[str, str]:
    """A deliberately small .po reader: msgid/msgstr pairs, no plurals, no context."""
    entries: dict[str, str] = {}
    msgid: str | None = None
    msgstr: str | None = None
    current: str | None = None

    def flush() -> None:
        nonlocal msgid, msgstr, current
        if msgid is not None and msgid != "":
            entries[msgid] = msgstr or ""
        msgid = msgstr = current = None

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            if not line:
                flush()
            continue
        if line.startswith("msgid "):
            flush()
            msgid = _unquote(line[6:])
            current = "id"
        elif line.startswith("msgstr "):
            msgstr = _unquote(line[7:])
            current = "str"
        elif line.startswith('"'):
            if current == "id" and msgid is not None:
                msgid += _unquote(line)
            elif current == "str" and msgstr is not None:
                msgstr += _unquote(line)
    flush()
    return entries


def _unquote(chunk: str) -> str:
    chunk = chunk.strip()
    if chunk.startswith('"') and chunk.endswith('"'):
        chunk = chunk[1:-1]
    return chunk.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")


def main() -> int:
    catalogs: dict[str, dict[str, str]] = {}
    for po in sorted(LOCALE_DIR.glob("*/LC_MESSAGES/biokids.po")):
        locale = po.parent.parent.name
        catalogs[locale] = parse_po(po)

    if len(catalogs) < 2:
        print(f"expected at least two catalogs under {LOCALE_DIR}, found {len(catalogs)}")
        return 1

    problems: list[str] = []
    all_keys: set[str] = set()
    for entries in catalogs.values():
        all_keys |= set(entries)

    for locale, entries in sorted(catalogs.items()):
        missing = sorted(all_keys - set(entries))
        for key in missing:
            problems.append(f"{locale}: missing key {key!r}")
        for key, value in sorted(entries.items()):
            if not value.strip():
                problems.append(f"{locale}: empty translation for {key!r}")

    for problem in problems:
        print(problem)

    total = len(all_keys)
    if problems:
        print(f"\n{len(problems)} problem(s) across {total} keys")
        return 1

    print(f"i18n ok: {total} keys x {len(catalogs)} locales ({', '.join(sorted(catalogs))})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
