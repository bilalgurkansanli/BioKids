#!/usr/bin/env python3
"""Fail the build if a Python dependency carries a non-permissive license.

D-30 is a hard constraint, and a good intention decays. This turns it into a
test. Only licenses we can ship inside an MIT project are allowed; GPL software
we invoke as a separate process (dnsmasq, Chromium, Piper's binary) never shows
up here because it is not a Python dependency.
"""

from __future__ import annotations

import json
import subprocess
import sys

ALLOWED_TOKENS = (
    "mit",
    "bsd",
    "apache",
    "isc",
    "psf",
    "python software foundation",
    "mozilla public license 2.0",  # MPL-2.0 is file-level copyleft; acceptable
    "unlicense",
    "public domain",
    "zlib",
    "postgresql",  # the PostgreSQL license is a BSD variant
    "hpnd",
)

# Packages whose metadata is unhelpful ("UNKNOWN") but whose license is known
# good. Each entry needs a reason, so the exemption stays reviewable.
KNOWN_GOOD = {
    "biokids": "this project, MIT",
}


def main() -> int:
    raw = subprocess.run(
        [sys.executable, "-m", "piplicenses", "--format=json", "--with-system"],
        capture_output=True,
        text=True,
        check=False,
    )
    if raw.returncode != 0:
        raw = subprocess.run(
            ["pip-licenses", "--format=json", "--with-system"],
            capture_output=True,
            text=True,
            check=True,
        )

    packages = json.loads(raw.stdout)
    bad: list[tuple[str, str]] = []

    for pkg in packages:
        name = str(pkg.get("Name", "")).lower()
        license_text = str(pkg.get("License", "")).lower()
        if name in KNOWN_GOOD:
            continue
        if any(token in license_text for token in ALLOWED_TOKENS):
            continue
        bad.append((pkg.get("Name", "?"), pkg.get("License", "?")))

    if bad:
        print("Non-permissive or unrecognised dependency licenses (see D-30):\n")
        for name, lic in bad:
            print(f"  {name:<32} {lic}")
        print(
            "\nEither drop the dependency, invoke it as a separate process, or — if the "
            "metadata is simply wrong — add it to KNOWN_GOOD with a reason."
        )
        return 1

    print(f"license check ok: {len(packages)} packages, all permissive")
    return 0


if __name__ == "__main__":
    sys.exit(main())
