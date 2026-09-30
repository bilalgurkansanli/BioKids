#!/usr/bin/env python3
"""Fail on the packaging mistakes that only a real build would otherwise catch.

Every check here has already cost a build, and each mistake is invisible from
the source tree without a check like this one:

* `debian/biokids-ai.install` claimed `usr/lib/python3/dist-packages/biokids_ai`
  while `src/biokids_ai/` did not exist. `dh_install` fails the whole build on a
  glob that matches nothing, so one missing `__init__.py` took down all nine
  packages.

* `debian/rules` installed nothing for the child's XFCE session, so the units
  shipped with `--no-enable` and nothing ever enabled them. That build was
  perfectly green. The child simply logged in to an ordinary desktop.

* The first real `dpkg-buildpackage`, on a Debian 13 VM, failed in `dh_install`
  twice over. `PYBUILD_NAME` in `debian/rules` made pybuild install into
  `debian/python3-biokids` instead of `debian/tmp`; and pybuild installs into
  the versioned `python3.13/dist-packages`, which `dh_python3` renames only
  after `dh_install` has run - so `usr/lib/python3/dist-packages/...` in a
  `.install` file matches nothing even in the right directory.

* `biokids-provision@.service` said `Documentation=man:biokids-admin(8)` for a
  page nobody had written. `systemd-analyze verify` was the first thing to
  notice, on the VM; `systemctl help` would have been the second.

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
#: that is what `_installed_by_rules` reads. The glob is not a style choice: at
#: `dh_install` time the directory is `python3.13/dist-packages`.
DIST_PACKAGES = "usr/lib/python3*/dist-packages/"
UNVERSIONED_DIST_PACKAGES = "usr/lib/python3/dist-packages/"
BIN = "usr/bin/"


def problems() -> list[str]:
    found: list[str] = []
    found += _install_files_name_things_that_exist()
    found += _every_binary_package_has_an_install_file()
    found += _units_referenced_are_shipped()
    found += _pybuild_installs_into_debian_tmp()
    found += _manual_pages_exist()
    found += _debhelper_marker_once()
    return found


# ------------------------------------------------------------------ the checks


def _install_files_name_things_that_exist() -> list[str]:
    """Every path in a `.install` file must be producible.

    Three kinds of line appear, and each is checkable without building:
    `usr/lib/python3*/dist-packages/<module>` needs `src/<module>/`;
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

            if source.startswith(UNVERSIONED_DIST_PACKAGES):
                out.append(
                    f"{where}: {source} matches nothing at dh_install time. pybuild "
                    "installs into python3.X/dist-packages and dh_python3 renames it "
                    f"later; write {DIST_PACKAGES}..."
                )
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


def _pybuild_installs_into_debian_tmp() -> list[str]:
    """`PYBUILD_NAME` redirects the whole Python install away from debian/tmp.

    pybuild treats it as naming the one binary package, `python3-<name>`, and
    replaces a destination of `debian/tmp` with `debian/python3-<name>` - which
    no `.install` file reads from. A source package that splits one Python
    distribution across several binary packages must not set it.
    """
    rules = (DEBIAN / "rules").read_text(encoding="utf-8")
    if re.search(r"^\s*(export\s+)?PYBUILD_NAME\s*[:?]?=", rules, re.M):
        return [
            "rules: PYBUILD_NAME is set, so pybuild installs into "
            "debian/python3-<name> instead of debian/tmp and every .install file "
            "finds nothing"
        ]
    return []


def _manual_pages_exist() -> list[str]:
    """Every page a `.manpages` file lists, and every page a unit points at.

    A unit's `Documentation=man:page(N)` is what `systemctl help` opens, so a
    reference to a page that is not shipped is a dead end at exactly the moment
    somebody needs it. Pages live in `debian/man/`.
    """
    out: list[str] = []
    shipped: set[str] = set()
    for manifest in sorted(DEBIAN.glob("*.manpages")):
        for number, raw in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if not (ROOT / line).is_file():
                out.append(f"{manifest.name}:{number}: {line} does not exist")
                continue
            shipped.add(Path(line).name)

    for unit in sorted(DEBIAN.glob("*.service")):
        text = unit.read_text(encoding="utf-8")
        for page, section in re.findall(r"man:([A-Za-z0-9_.@-]+)\((\d)\)", text):
            if f"{page}.{section}" not in shipped:
                out.append(
                    f"{unit.name}: Documentation=man:{page}({section}), but no "
                    f".manpages file ships {page}.{section}"
                )
    return out


MAINTAINER_SCRIPTS = ("preinst", "postinst", "prerm", "postrm")


def _debhelper_marker_once() -> list[str]:
    """The debhelper marker, exactly once, on a line of its own.

    debhelper substitutes the *first* occurrence it finds. Two prerm scripts
    mentioned it in a comment above the real one, so the generated code was
    spliced into the middle of the comment and the comment's tail ran as a
    command: `prerm: 20: block: not found`, on every removal (D-159).
    """
    marker = "#" + "DEBHELPER" + "#"
    out: list[str] = []
    for script in sorted(DEBIAN.iterdir()):
        if script.suffix.lstrip(".") not in MAINTAINER_SCRIPTS:
            continue
        lines = script.read_text(encoding="utf-8").splitlines()
        mentions = [n for n, line in enumerate(lines, 1) if marker in line]
        own = [n for n in mentions if lines[n - 1].strip() == marker]
        if len(mentions) != 1 or len(own) != 1:
            out.append(
                f"{script.name}: the debhelper marker must appear exactly once, alone on "
                f"its line (found on lines {mentions or 'none'}); debhelper replaces the "
                "first one, comment or not"
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
