#!/usr/bin/env python3
"""Fail on the packaging mistakes that only a real build would otherwise catch.

Two of these have already cost a build each, and both are invisible from the
source tree without a check like this one:

* `debian/biokids-ai.install` claimed `usr/lib/python3/dist-packages/biokids_ai`
  while `src/biokids_ai/` did not exist. `dh_install` fails the whole build on a
  glob that matches nothing, so one missing `__init__.py` took down all nine
  packages.

* `debian/rules` installed nothing for the child's XFCE session, so the units
  shipped with `--no-enable` and nothing ever enabled them. That build was
  perfectly green. The child simply logged in to an ordinary desktop.

So the rule this enforces is "everything the packaging names must exist, and
everything shipped must be claimed by exactly one package". `dh_missing
--fail-missing` already enforces the second half at build time; this runs on a
laptop with no `dpkg-buildpackage`, in under a second, on every push.

It is deliberately not clever. It reads paths, not semantics, and every check
here maps to a mistake that has actually been made.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEBIAN = ROOT / "debian"
SRC = ROOT / "src"

#: What `dh_auto_install` puts under `debian/tmp` by itself, given pyproject's
#: packages and scripts. Anything else has to be placed by `debian/rules`, and
#: that is what `_installed_by_rules` reads.
DIST_PACKAGES = "usr/lib/python3/dist-packages/"
BIN = "usr/bin/"


def problems() -> list[str]:
    found: list[str] = []
    found += _install_files_name_things_that_exist()
    found += _every_binary_package_has_an_install_file()
    found += _units_referenced_are_shipped()
    return found


# ------------------------------------------------------------------ the checks


def _install_files_name_things_that_exist() -> list[str]:
    """Every path in a `.install` file must be producible.

    Three kinds of line appear, and each is checkable without building:
    `usr/lib/python3/dist-packages/<module>` needs `src/<module>/`;
    `usr/bin/<script>` needs an entry point in pyproject.toml; and a line with
    a `debian/...` source needs that file to be in the tree.
    """
    out: list[str] = []
    scripts = _console_scripts()
    by_rules = _installed_by_rules()

    for manifest in sorted(DEBIAN.glob("*.install")):
        for number, raw in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            source = line.split()[0]
            where = f"{manifest.name}:{number}"

            if source.startswith("debian/"):
                if not (ROOT / source).exists():
                    out.append(f"{where}: {source} does not exist")
                continue

            if source.startswith(DIST_PACKAGES):
                module = source[len(DIST_PACKAGES) :]
                if "*" in module:
                    # `biokids-*` is the dist-info directory pybuild writes.
                    continue
                if not (SRC / module).is_dir():
                    out.append(
                        f"{where}: claims {source}, but src/{module}/ does not exist. "
                        "dh_install fails the whole build on a glob that matches nothing"
                    )
                continue

            if source.startswith(BIN):
                name = source[len(BIN) :]
                if name not in scripts:
                    out.append(
                        f"{where}: claims {source}, but pyproject.toml declares no "
                        f"[project.scripts] entry named {name!r}"
                    )
                continue

            if not any(source.startswith(path) for path in by_rules):
                out.append(
                    f"{where}: nothing in debian/rules installs {source} into debian/tmp"
                )
    return out


def _every_binary_package_has_an_install_file() -> list[str]:
    """A package with files but no manifest ships empty, and says nothing about it.

    Metapackages are the exception and are recognised by having no Depends on
    themselves to install - `Section: metapackages` is the marker debian/control
    already uses.
    """
    out: list[str] = []
    control = (DEBIAN / "control").read_text(encoding="utf-8")
    for stanza in control.split("\n\n"):
        name = _field(stanza, "Package")
        if not name:
            continue
        if _field(stanza, "Section") == "metapackages":
            continue
        if not (DEBIAN / f"{name}.install").is_file():
            out.append(f"control: {name} has no debian/{name}.install, so it ships empty")
    return out


def _units_referenced_are_shipped() -> list[str]:
    """A unit named in debian/rules but absent from debian/ is a silent no-op.

    `dh_installsystemd --name=X` looks for exactly one filename and has no
    fallback, so a typo produces a package with no unit and no error.
    """
    out: list[str] = []
    rules = (DEBIAN / "rules").read_text(encoding="utf-8")
    for package, unit in re.findall(r"-p(\S+)\s+--name=(\S+)", rules):
        wanted = DEBIAN / f"{package}.{unit}.service"
        if not wanted.is_file():
            out.append(
                f"rules: dh_installsystemd --name={unit} reads {wanted.name}, "
                "which does not exist; the package would ship no unit at all"
            )
    return out


# ------------------------------------------------------------------- reading


def _field(stanza: str, name: str) -> str:
    match = re.search(rf"^{name}:\s*(.+)$", stanza, re.M)
    return match.group(1).strip() if match else ""


def _console_scripts() -> set[str]:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    section = text.split("[project.scripts]", 1)
    if len(section) < 2:
        return set()
    body = section[1].split("\n[", 1)[0]
    return set(re.findall(r"^([A-Za-z0-9_.-]+)\s*=", body, re.M))


def _installed_by_rules() -> set[str]:
    """Destinations under `debian/tmp/` that `debian/rules` creates by hand."""
    rules = (DEBIAN / "rules").read_text(encoding="utf-8")
    found: set[str] = set()
    for path in re.findall(r"debian/tmp/(\S+)", rules):
        cleaned = path.rstrip("\\").rstrip("/")
        # `$$locale` and friends: keep the fixed prefix, drop the variable tail.
        cleaned = cleaned.split("$")[0].rstrip("/")
        if cleaned:
            found.add(cleaned)
    return found


def main() -> int:
    found = problems()
    for problem in found:
        print(problem)
    if found:
        print(f"\n{len(found)} packaging problem(s)")
        return 1
    print("packaging ok: every path the packaging names exists or is built")
    return 0


if __name__ == "__main__":
    sys.exit(main())
