#!/usr/bin/env python3
"""Refresh the snapshot of blocklists that ships inside biokids-core (D-167).

The child's DNS filter must work on a machine that has never been online since
it was installed, so a snapshot of each list is committed here, compressed, and
installed with the package. This script is how that snapshot is renewed: run
it, look at the diff of `SOURCES.toml`, commit.

Every list is from ShadowWhisperer/BlockLists, which is released into the
public domain (the Unlicense) - checked again each time this runs, and the run
stops if the licence file changes. Pinned to the commit it fetched, so a
snapshot can always be traced back to exactly what it was built from.
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "src" / "biokids_core" / "data" / "blocklists"
REPO = "ShadowWhisperer/BlockLists"
LICENCE_START = "This is free and unencumbered software released into the public domain."

#: Our category -> the list files that make it up.
CATEGORIES = {
    "adult": ["Adult"],
    "gambling": ["Gambling"],
    "malware": ["Malware", "Scam"],
    "doh": ["DNS"],
}

DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9-]{1,63}\.)+[a-z][a-z0-9-]{1,62}$")


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "biokids-dev"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return bytes(response.read())


def main() -> int:
    commit = json.loads(fetch(f"https://api.github.com/repos/{REPO}/commits?per_page=1"))[0]
    sha = commit["sha"]
    licence = fetch(f"https://raw.githubusercontent.com/{REPO}/{sha}/LICENSE").decode()
    if not licence.startswith(LICENCE_START):
        print("the licence of the source changed; stopping", file=sys.stderr)
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    lines = [
        "# The DNS filter's built-in lists (D-167). Written by dev/update_blocklists.py.",
        f'source = "https://github.com/{REPO}"',
        f'commit = "{sha}"',
        f'fetched = "{dt.date.today().isoformat()}"',
        'licence = "Unlicense (public domain)"',
        "",
    ]
    for category, files in CATEGORIES.items():
        domains: set[str] = set()
        for name in files:
            raw = fetch(f"https://raw.githubusercontent.com/{REPO}/{sha}/Lists/{name}")
            for line in raw.decode("utf-8", "replace").splitlines():
                word = line.strip().lower().removeprefix("*.")
                if word and not word.startswith("#") and DOMAIN.match(word):
                    domains.add(word)
        body = "\n".join(sorted(domains)).encode() + b"\n"
        (OUT / f"{category}.txt.gz").write_bytes(gzip.compress(body, 9, mtime=0))
        lines.append(f"[{category}]")
        lines.append(f"files = {json.dumps(files)}")
        lines.append(f"domains = {len(domains)}")
        lines.append("")
        print(f"{category}: {len(domains)} domains")
    (OUT / "SOURCES.toml").write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
