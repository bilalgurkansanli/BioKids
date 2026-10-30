"""`biokids-ai-setup`: fetch the assistant's models once, and check them.

Run by `biokids-ai-setup.service` when a parent presses "Download" in the panel,
as the `biokids-ai` user, with the network the assistant itself never has
(D-165). Every file is pinned to a Hugging Face commit and checked against its
SHA-256 before it is moved into place; a file that does not match is deleted,
not used. An interrupted download resumes where it stopped.

    biokids-ai-setup status       what is installed, chosen and recommended
    biokids-ai-setup download     fetch the recommended models (or --chat/--stt)
    biokids-ai-setup remove       delete every model file

The models directory holds the files and `selection.json`, which names the chat
and speech model the service should load.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from biokids_ai.models import CATALOG, ModelFile, by_key, physical_ram_gb, recommend

log = logging.getLogger("biokids-ai-setup")

MODELS_DIR: Final = Path("/var/lib/biokids-ai/models")
SELECTION_FILE: Final = "selection.json"
PROGRESS_FILE: Final = "download.json"
"""How far the download is, for the panel's progress bar (D-254): the state
(`running`, `done`, `failed`), the bytes fetched and to fetch over every file of
this download, and the file being fetched. The daemon reads it."""
PROGRESS_EVERY: Final = 1 << 23
"""Rewrite the progress file after this many more bytes (8 MiB)."""
CHUNK: Final = 1 << 20
TIMEOUT_S: Final = 60

Opener = Callable[[urllib.request.Request], Any]


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def installed(model: ModelFile, models_dir: Path) -> bool:
    """Present and the right size. The hash was checked when it arrived."""
    path = models_dir / model.filename
    try:
        return path.stat().st_size == model.size
    except OSError:
        return False


def read_selection(models_dir: Path) -> dict[str, str]:
    try:
        raw = json.loads((models_dir / SELECTION_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def write_selection(models_dir: Path, selection: dict[str, str]) -> None:
    partial = models_dir / (SELECTION_FILE + ".part")
    partial.write_text(json.dumps(selection, indent=2) + "\n", encoding="utf-8")
    partial.replace(models_dir / SELECTION_FILE)


def write_progress(models_dir: Path, **progress: object) -> None:
    """The progress file, replaced whole so that a reader never sees half of it."""
    models_dir.mkdir(parents=True, exist_ok=True)
    partial = models_dir / (PROGRESS_FILE + ".part")
    partial.write_text(json.dumps(progress) + "\n", encoding="utf-8")
    partial.replace(models_dir / PROGRESS_FILE)


def read_progress(models_dir: Path) -> dict[str, Any]:
    """The last progress written, or {} when no download was ever started."""
    try:
        data = json.loads((models_dir / PROGRESS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def download(
    model: ModelFile,
    models_dir: Path,
    *,
    opener: Opener = urllib.request.urlopen,
    progress: Callable[[int, int], None] | None = None,
) -> Path:
    """Fetch one file, resuming a partial one, and check it. Raises on any failure."""
    models_dir.mkdir(parents=True, exist_ok=True)
    final = models_dir / model.filename
    if installed(model, models_dir):
        return final
    partial = models_dir / (model.filename + ".part")
    have = partial.stat().st_size if partial.exists() else 0
    if have > model.size:
        partial.unlink()
        have = 0

    if have < model.size:
        # Always https://huggingface.co/<repo>/resolve/<pinned commit>/...: the
        # URL comes from the reviewed CATALOG, never from input.
        request = urllib.request.Request(  # noqa: S310
            model.url, headers={"User-Agent": "biokids-ai-setup"}
        )
        if have:
            request.add_header("Range", f"bytes={have}-")
        with opener(request) as response:
            status = getattr(response, "status", 200)
            if have and status != 206:
                # The server ignored the range: start again rather than append
                # a whole file to half of one.
                have = 0
                partial.unlink(missing_ok=True)
            with partial.open("ab") as out:
                while True:
                    block = response.read(CHUNK)
                    if not block:
                        break
                    out.write(block)
                    have += len(block)
                    if progress is not None:
                        progress(have, model.size)

    actual = sha256_of(partial)
    if actual != model.sha256:
        partial.unlink(missing_ok=True)
        raise ValueError(
            f"{model.filename}: checksum {actual} does not match the pinned {model.sha256}"
        )
    partial.replace(final)
    final.chmod(0o640)
    return final


def status(models_dir: Path, ram_gb: float) -> dict[str, object]:
    chosen = recommend(ram_gb)
    return {
        "ram_gb": round(ram_gb, 1),
        "recommended": {role: m.key for role, m in chosen.items()},
        "installed": [m.key for m in CATALOG if installed(m, models_dir)],
        "selection": read_selection(models_dir),
        "downloading": [
            m.key for m in CATALOG if (models_dir / (m.filename + ".part")).exists()
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="biokids-ai-setup", description=__doc__.split("\n")[0]
    )
    parser.add_argument("--models-dir", type=Path, default=MODELS_DIR)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    get = sub.add_parser("download")
    get.add_argument("--chat", help="a chat model key instead of the recommended one")
    get.add_argument("--stt", help="a speech model key instead of the recommended one")
    sub.add_parser("remove")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    ram = physical_ram_gb()
    if args.command == "status":
        print(json.dumps(status(args.models_dir, ram), indent=2))
        return 0

    if args.command == "remove":
        for model in CATALOG:
            for name in (model.filename, model.filename + ".part"):
                with contextlib.suppress(OSError):
                    (args.models_dir / name).unlink()
        with contextlib.suppress(OSError):
            (args.models_dir / SELECTION_FILE).unlink()
        log.info("every assistant model file was removed")
        return 0

    chosen = recommend(ram)
    for role, override in (("chat", args.chat), ("stt", args.stt)):
        if override:
            picked = by_key(override)
            if picked is None or picked.role != role:
                log.error("%s is not a %s model", override, role)
                return 2
            chosen[role] = picked
    if "chat" not in chosen:
        log.error("this computer has %.1f GB of memory, too little for the assistant", ram)
        return 3

    selection = read_selection(args.models_dir)
    # The bar covers the whole download: what is already here, whole or in
    # part, counts as done from the start.
    total = sum(model.size for model in chosen.values())
    done_before = 0
    for model in chosen.values():
        partial = args.models_dir / (model.filename + ".part")
        if installed(model, args.models_dir):
            done_before += model.size
        elif partial.exists():
            done_before += min(partial.stat().st_size, model.size)
    write_progress(args.models_dir, state="running", done=done_before, total=total, file="")
    finished = 0
    for role, model in chosen.items():
        log.info("downloading %s (%d MB, %s)", model.label, model.size_mb, model.licence)
        last = [0, 0]

        def report(
            done: int,
            size: int,
            last: list[int] = last,
            model: ModelFile = model,
            finished: int = finished,
        ) -> None:
            percent = done * 100 // max(size, 1)
            if percent >= last[0] + 5:
                last[0] = percent
                log.info("  %d%%", percent)
            if done - last[1] >= PROGRESS_EVERY:
                last[1] = done
                write_progress(
                    args.models_dir,
                    state="running",
                    done=finished + done,
                    total=total,
                    file=model.label,
                )

        already = installed(model, args.models_dir)
        try:
            download(model, args.models_dir, progress=report)
        except (OSError, urllib.error.URLError, ValueError) as exc:
            log.error("%s could not be downloaded: %s", model.filename, exc)
            write_progress(
                args.models_dir, state="failed", done=finished, total=total, file=model.label
            )
            return 1
        finished += model.size
        if already:
            log.info("  already here")
        selection[role] = model.key
        write_selection(args.models_dir, selection)
    write_progress(args.models_dir, state="done", done=total, total=total, file="")
    log.info("done")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
