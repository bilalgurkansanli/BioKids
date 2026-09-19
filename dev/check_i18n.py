#!/usr/bin/env python3
"""Fail if the translation catalogs have drifted apart.

Every message id present in one locale must be present in all of them, and no
catalog may carry an empty translation. This is the guard behind T1.3: a missing
Turkish string is a bug the same way a missing function is.

It also checks the audit vocabulary, which parity alone cannot catch. The audit
screen renders `_('audit.action.' ~ entry.action)` and gettext returns the key
unchanged when nothing matches, so an action added in Python with no catalogue
entry does not fail anywhere - it just prints its own slug. Twelve of them had
accumulated that way, and a parent reading their family's history saw
`passphrase_failed` and `access_requested` on the one screen whose whole job is
to be readable.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCALE_DIR = ROOT / "src" / "biokids_core" / "locale"
SRC_DIR = ROOT / "src"

AUDIT_CALLS = {"audit", "audit_now"}


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


def audited_actions() -> set[str]:
    """Every action literal passed to `repo.audit(...)`, read from the syntax tree.

    An AST rather than a regex, because `action=` is also argparse's keyword:
    a pattern loose enough to catch `action="protection_enabled" if ... else ...`
    also catches `add_argument("--stdin", action="store_true")` and then demands
    a Turkish sentence for `store_true`. Here the call is identified first and
    the keyword read second, so only real audit entries are checked.

    Both branches of a conditional are collected. An action assembled at runtime
    is invisible to this, and should stay that way: an audited action that
    cannot be read off the source cannot be translated ahead of time either.
    """
    found: set[str] = set()

    def literals(node: ast.expr) -> set[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        if isinstance(node, ast.IfExp):
            return literals(node.body) | literals(node.orelse)
        return set()

    for path in sorted(SRC_DIR.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:  # pragma: no cover - a broken file fails elsewhere
            print(f"{path}: {exc}")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name not in AUDIT_CALLS:
                continue
            for keyword in node.keywords:
                if keyword.arg == "action":
                    found |= literals(keyword.value)
    return found


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

    actions = audited_actions()
    for action in sorted(actions):
        if f"audit.action.{action}" not in all_keys:
            problems.append(
                f"audit: {action!r} is written to the log but has no "
                f"'audit.action.{action}' string, so the screen shows the slug"
            )

    for problem in problems:
        print(problem)

    total = len(all_keys)
    if problems:
        print(f"\n{len(problems)} problem(s) across {total} keys")
        return 1

    print(f"i18n ok: {total} keys x {len(catalogs)} locales ({', '.join(sorted(catalogs))})")
    print(f"audit ok: {len(actions)} audited actions, all with a translated name")
    return 0


if __name__ == "__main__":
    sys.exit(main())
