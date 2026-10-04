"""Which model files the assistant uses, where they come from, and how they are checked.

D-165. Nothing is bundled in a package: the files are hundreds of megabytes to
gigabytes, and a family that never turns the assistant on should never carry
them. A parent's "download" fetches them once from Hugging Face - the
destination `docs/SECURITY.md` section 4 already lists for this package -
pinned to a commit and checked against its SHA-256, so the file that arrives is
the file that was reviewed. After that the assistant runs with no network at all.

Every model here is under a permissive licence (Apache-2.0 or MIT), checked on
the model card when it was pinned. Licences that restrict use - the Llama
Community License, Gemma Terms of Use, the Qwen Research License - are not
eligible (CLAUDE.md section 6).

There is no model for computers with less than 7 GB of memory, on purpose. Qwen
3.5 2B was tried for them and told a six-year-old that dinosaurs married people:
a child is better served by no assistant than by one that is confidently wrong
(D-165). The panel says the computer has too little memory instead.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

HF: Final = "https://huggingface.co"


@dataclass(frozen=True, slots=True)
class ModelFile:
    key: str
    """Stable name for the selection file and the panel. Never renamed."""
    role: str
    """`chat` | `stt`."""
    repo: str
    revision: str
    filename: str
    size: int
    sha256: str
    licence: str
    min_ram_gb: float
    """Below this much physical memory the file is not offered."""
    label: str
    """What the parent sees, in plain words."""

    @property
    def url(self) -> str:
        return f"{HF}/{self.repo}/resolve/{self.revision}/{self.filename}"

    @property
    def size_mb(self) -> int:
        return round(self.size / 1_000_000)


CATALOG: Final[tuple[ModelFile, ...]] = (
    ModelFile(
        key="gemma-4-e4b",
        role="chat",
        repo="google/gemma-4-E4B-it-qat-q4_0-gguf",
        revision="4b4a2c1d584be7264f87aac328a1bc739ce81b6c",
        filename="gemma-4-E4B_q4_0-it.gguf",
        size=5_154_941_280,
        sha256="676c35070db6dbe52f93e9c864ee0fba4eddea94b9c875d9cb10daff453fbaee",
        licence="Apache-2.0",
        min_ram_gb=12.0,
        label="Gemma 4 E4B (Google) - the best Turkish, for computers with 12 GB or more",
    ),
    ModelFile(
        key="gemma-4-e2b",
        role="chat",
        repo="google/gemma-4-E2B-it-qat-q4_0-gguf",
        revision="675cff42a74c774d6cb76f76d8eacb49b48c9b93",
        filename="gemma-4-E2B_q4_0-it.gguf",
        size=3_349_516_256,
        sha256="fa401b55b07ee70a54c6dae3903c783a6e65064312529ea57175cb5f8dec6634",
        licence="Apache-2.0",
        min_ram_gb=7.0,
        label="Gemma 4 E2B (Google) - best Turkish of the small models",
    ),
    ModelFile(
        key="whisper-turbo",
        role="stt",
        repo="ggerganov/whisper.cpp",
        revision="5359861c739e955e79d9a303bcbc70fb988958b1",
        filename="ggml-large-v3-turbo-q5_0.bin",
        size=574_041_195,
        sha256="394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2",
        licence="MIT",
        min_ram_gb=12.0,
        label="Whisper large-v3-turbo (OpenAI) - better Turkish, needs more memory",
    ),
    ModelFile(
        key="whisper-small",
        role="stt",
        repo="ggerganov/whisper.cpp",
        revision="5359861c739e955e79d9a303bcbc70fb988958b1",
        filename="ggml-small-q5_1.bin",
        size=190_085_487,
        sha256="ae85e4a935d7a567bd102fe55afc16bb595bdb618e11b2fc7591bc08120411bb",
        licence="MIT",
        min_ram_gb=3.0,
        label="Whisper small (OpenAI) - understands spoken Turkish",
    ),
)


def by_key(key: str) -> ModelFile | None:
    return next((m for m in CATALOG if m.key == key), None)


def physical_ram_gb() -> float:
    """Total memory, from /proc/meminfo; 0 when it cannot be read."""
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) / 1024 / 1024
    except (OSError, ValueError, IndexError):
        pass
    try:
        sysconf = getattr(os, "sysconf")  # noqa: B009 - POSIX only
        return float(sysconf("SC_PAGE_SIZE") * sysconf("SC_PHYS_PAGES")) / 1024**3
    except (ValueError, OSError, AttributeError):
        return 0.0


def recommend(ram_gb: float) -> dict[str, ModelFile]:
    """The best chat and speech model this machine can hold, by role.

    "Best" is the order of CATALOG: within a role, the first entry the memory
    allows. A machine below every threshold gets nothing, and the panel says
    the assistant needs more memory rather than installing something that makes
    the computer feel broken (docs/AI-LAYER.md section 10).
    """
    chosen: dict[str, ModelFile] = {}
    for model in CATALOG:
        if model.role not in chosen and ram_gb >= model.min_ram_gb:
            chosen[model.role] = model
    return chosen
