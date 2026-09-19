#!/usr/bin/env python3
"""Compile every `.po` catalog into a binary `.mo`, without `msgfmt`.

`debian/rules` calls this during the build. Shelling out to `msgfmt` would mean a
build dependency on `gettext` on Debian and a missing binary on the Windows host
this project is developed from — for a file format that is a header, two offset
tables and two blobs of NUL-terminated strings, unchanged since 1995. So the
format is implemented here directly and verified by loading the result back
through the same `gettext` module the application uses at runtime.

Parsing is *not* implemented here: `biokids_core.i18n.parse_po` already reads the
catalogs the application will read, and a second parser would eventually disagree
with the first one about some escape sequence. `dev/check_i18n.py` keeps the
locales in step with each other; this script only translates a format.

Exit status is non-zero if a catalog cannot be read, or if what `gettext` reads
back from the compiled file differs by a single byte from what went in.

`--clean` deletes the compiled catalogs again. It is the counterpart of
`override_dh_auto_clean` in `debian/rules`, and it matters in a source tree:
`i18n.load_catalog` prefers a `.mo` as soon as one exists, so a stray compiled
catalog turns every later `.po` edit into a string that quietly refuses to
change.
"""

from __future__ import annotations

import argparse
import gettext
import struct
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Final

ROOT: Final = Path(__file__).resolve().parent.parent
# `debian/rules` runs this straight from the unpacked source, before biokids-core
# is installed anywhere Python would find it.
sys.path.insert(0, str(ROOT / "src"))

from biokids_core import i18n as core_i18n  # noqa: E402
from biokids_core.i18n import DOMAIN, parse_po  # noqa: E402

DEFAULT_LOCALE_DIR: Final = ROOT / "src" / "biokids_core" / "locale"

MAGIC: Final = 0x950412DE
"""The little-endian GNU `.mo` magic. A big-endian file swaps every word."""

REVISION: Final = 0
"""Format revision 0. Revision 1 exists on paper and no implementation reads it."""

HEADER_WORDS: Final = 7
"""magic, revision, count, key table, value table, hash size, hash offset."""


def build_mo(messages: Mapping[str, str], metadata: str) -> bytes:
    """Pack messages into the GNU `.mo` layout.

    Entries are sorted by msgid so the empty one — the metadata — lands first.
    That ordering is not cosmetic: `gettext` reads entries in table order and
    only learns the catalog's charset from the metadata entry, so anything
    placed before it would be decoded with the wrong codec. The hash table is
    written as absent (size 0), which every reader since glibc 2.0 accepts and
    which keeps the file reproducible.
    """
    entries: list[tuple[bytes, bytes]] = [(b"", metadata.encode("utf-8"))]
    # An empty key would collide with the metadata entry and give the file two
    # rows with the same msgid; `parse_po` never produces one, but this function
    # should not depend on that.
    entries += [(k.encode("utf-8"), v.encode("utf-8")) for k, v in messages.items() if k]
    entries.sort(key=lambda pair: pair[0])

    count = len(entries)
    key_table_offset = HEADER_WORDS * 4
    value_table_offset = key_table_offset + count * 8
    keys_offset = value_table_offset + count * 8
    # Every string is stored with a trailing NUL that is not counted in its
    # length, so the values start after the keys plus one byte each.
    values_offset = keys_offset + sum(len(key) + 1 for key, _ in entries)

    key_table = bytearray()
    value_table = bytearray()
    keys = bytearray()
    values = bytearray()
    for key, value in entries:
        key_table += struct.pack("<II", len(key), keys_offset + len(keys))
        keys += key + b"\0"
        value_table += struct.pack("<II", len(value), values_offset + len(values))
        values += value + b"\0"

    header = struct.pack(
        "<7I",
        MAGIC,
        REVISION,
        count,
        key_table_offset,
        value_table_offset,
        0,  # hash table size
        0,  # hash table offset
    )
    return bytes(header + key_table + value_table + keys + values)


def metadata_for(locale: str) -> str:
    """The entry stored under the empty msgid.

    Regenerated rather than copied out of the `.po` header, because the only
    field a reader acts on is the charset and BioKids' catalogs are UTF-8 by
    construction. `parse_po` drops the empty msgid for the same reason: it is
    metadata about the file, not a message the product can display.
    """
    return (
        "Project-Id-Version: biokids\n"
        f"Language: {locale}\n"
        "MIME-Version: 1.0\n"
        "Content-Type: text/plain; charset=UTF-8\n"
        "Content-Transfer-Encoding: 8bit\n"
    )


def verify(mo_path: Path, messages: Mapping[str, str]) -> list[str]:
    """Read the compiled file back with `gettext` and compare every message."""
    problems: list[str] = []
    with mo_path.open("rb") as handle:
        translations = gettext.GNUTranslations(handle)

    charset = translations.info().get("content-type", "")
    if "charset=UTF-8" not in charset:
        problems.append(f"{mo_path}: metadata says {charset!r}, expected UTF-8")

    for key, expected in messages.items():
        # A missing key makes gettext return the key itself, so this catches
        # absent entries as well as wrong ones.
        actual = translations.gettext(key)
        if actual != expected:
            problems.append(f"{mo_path}: {key!r} -> {actual!r}, expected {expected!r}")
    return problems


def verify_through_loader(locale: str, messages: Mapping[str, str]) -> list[str]:
    """Check what the application itself now sees.

    `i18n.load_catalog` prefers the `.mo` the moment it exists, so this is the
    only assertion that proves compiling a catalog does not change the set of
    keys the product can resolve. Skipped when compiling a tree other than the
    installed package's own.
    """
    core_i18n.reset_cache()
    loaded = core_i18n.load_catalog(locale).keys()
    expected = frozenset(messages)
    problems = [
        f"{locale}: {key!r} lost after compilation" for key in sorted(expected - loaded)
    ]
    problems += [
        f"{locale}: {key!r} appeared from nowhere" for key in sorted(loaded - expected)
    ]
    return problems


def compile_locale(po_path: Path, mo_path: Path, locale: str) -> tuple[dict[str, str], int]:
    """Compile one catalog. Returns the messages and the number of bytes written."""
    messages = parse_po(po_path)
    blob = build_mo(messages, metadata_for(locale))
    mo_path.parent.mkdir(parents=True, exist_ok=True)
    mo_path.write_bytes(blob)
    return messages, len(blob)


def clean(sources: list[Path], *, quiet: bool) -> int:
    """Remove the compiled catalogs, restoring the no-compile-step edit loop.

    `i18n.load_catalog` prefers a `.mo` the moment one exists, so a `.mo` left in
    a source tree turns every later `.po` edit into a string that does not
    change — the exact opposite of the promise in `CONTRIBUTING.md`. The package
    build has `override_dh_auto_clean` for this; a developer who ran the compiler
    by hand needs a way back too.
    """
    removed = 0
    for po_path in sources:
        mo_path = po_path.with_suffix(".mo")
        if mo_path.is_file():
            mo_path.unlink()
            removed += 1
            if not quiet:
                print(f"removed  {mo_path}")
    if not quiet:
        print(f"{removed} compiled catalog(s) removed; the .po files are read directly again")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compile the .po catalogs into binary .mo.")
    parser.add_argument(
        "--locale-dir",
        type=Path,
        default=DEFAULT_LOCALE_DIR,
        help="directory holding <locale>/LC_MESSAGES/<domain>.po",
    )
    parser.add_argument("--domain", default=DOMAIN, help="catalog domain")
    parser.add_argument("--quiet", action="store_true", help="only report problems")
    parser.add_argument(
        "--clean",
        action="store_true",
        help="delete the compiled catalogs instead of writing them",
    )
    args = parser.parse_args(argv)

    locale_dir: Path = args.locale_dir
    sources = sorted(locale_dir.glob(f"*/LC_MESSAGES/{args.domain}.po"))
    if not sources:
        print(f"no {args.domain}.po found under {locale_dir}", file=sys.stderr)
        return 1

    if args.clean:
        return clean(sources, quiet=args.quiet)

    problems: list[str] = []
    total = 0
    for po_path in sources:
        locale = po_path.parent.parent.name
        mo_path = po_path.with_suffix(".mo")
        try:
            messages, size = compile_locale(po_path, mo_path, locale)
        except OSError as exc:
            problems.append(f"{po_path}: {exc}")
            continue

        total += len(messages)
        if not args.quiet:
            kib = f"{size / 1024:.1f} KiB"
            print(f"{locale:<4} {len(messages):>4} messages  ->  {mo_path}  ({kib})")

        problems += verify(mo_path, messages)
        if locale_dir.resolve() == core_i18n.LOCALE_DIR.resolve():
            problems += verify_through_loader(locale, messages)

    if problems:
        print(f"\n{len(problems)} problem(s):", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1

    if not args.quiet:
        print(f"verified: {total} messages round-tripped through gettext.GNUTranslations")
        if locale_dir.resolve() == core_i18n.LOCALE_DIR.resolve():
            # Said out loud because the failure is silent: from here on the
            # application reads these files and not the .po beside them, so an
            # edited string simply will not appear until this runs again.
            print(f"note: the source tree now has .mo files, and {DOMAIN} reads those in")
            print("      preference to the .po. Run with --clean to get the edit loop back.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
