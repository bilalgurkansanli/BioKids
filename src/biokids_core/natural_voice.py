"""The natural voice: Piper and its Turkish voice, fetched when a parent asks (D-196).

espeak-ng is what Zio and Sio speak with on a stock machine, and it sounds like a
robot. Piper sounds like a person, and BioKids may ship neither half of it (D-137):
`piper-tts` is not in Debian 13, and the one Turkish voice in Piper's catalogue,
`tr_TR-dfki-medium`, was trained on a dataset licensed CC BY-NC-SA 4.0. A family may
use that at home; BioKids may not put it in a package. So the family fetches both
from their authors, with one button in the panel:

* Piper itself, the archived MIT-licensed 2023.11.14-2 release for x86_64 from
  `github.com/rhasspy/piper`, unpacked into `paths.piper_dir`
  (`/var/lib/biokids-voices/piper/`), where the voice service finds the program;
* the voice, from `huggingface.co/rhasspy/piper-voices` at a pinned commit, into
  `<paths.model_dir>/piper/`, with a link for each persona under the name
  `biokids_voice.synth.VoiceBank` looks for: `tr-zio.onnx` and `tr-sio.onnx`, each
  with its `.onnx.json` beside it, because Piper reads the model's settings from
  the model's own path plus `.json`.

The voice is a man's: the dfki recordings are of one male speaker, and there is no
Turkish woman in the catalogue. Sio speaks it as it is. Zio, the mother (D-85),
speaks the same voice raised, which the voice service does when it sees her model is
the same file as his (`VoiceBank.lift`).

Every file is pinned by its SHA-256 and size. Each goes to a temporary name in the
directory it will live in, is checked, and only then renamed into place; a file that
does not match is deleted and the setup fails. Nothing here touches the network but
`install` and the `fetch` it calls, and only `biokids-voice-setup.service` runs
that, when a parent presses the button (`docs/SECURITY.md` section 4). The panel
and the voice service only look at files.

    biokids-admin voice-setup download   fetch, check and install
    biokids-admin voice-setup status     what is there, as JSON
    biokids-admin voice-setup remove     delete what the download installed
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import shutil
import tarfile
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from typing import Any, Final

from biokids_core import remote
from biokids_core.config import Config
from biokids_core.errors import BioKidsError

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PinnedFile:
    """One file to fetch, exactly as it was when it was checked."""

    name: str
    """What it is called on this machine."""
    url: str
    sha256: str
    size: int


PIPER_VERSION: Final = "2023.11.14-2"
PIPER_RELEASE: Final = PinnedFile(
    name="piper_linux_x86_64.tar.gz",
    url=(
        f"https://github.com/rhasspy/piper/releases/download/{PIPER_VERSION}/"
        "piper_linux_x86_64.tar.gz"
    ),
    sha256="a50cb45f355b7af1f6d758c1b360717877ba0a398cc8cbe6d2a7a3a26e225992",
    size=26_460_462,
)
"""Piper 2023.11.14-2, MIT. The repository is archived, so this release is final."""

VOICES_COMMIT: Final = "c10ece1aade47bb51c153c893d14e5bf8e5b7117"
"""`rhasspy/piper-voices` as it was on 2026-10-08. A commit, not `main`: what is
fetched is what was checked, whatever the catalogue does next."""

TURKISH_VOICE: Final = "tr_TR-dfki-medium"
"""The only Turkish voice left in the catalogue (D-137). One male speaker, from the
DFKI "OT" recordings (github.com/marytts/dfki-ot-data), CC BY-NC-SA 4.0."""

_VOICE_URL: Final = (
    f"https://huggingface.co/rhasspy/piper-voices/resolve/{VOICES_COMMIT}/tr/tr_TR/dfki/medium"
)

VOICE_FILES: Final = (
    PinnedFile(
        name=f"{TURKISH_VOICE}.onnx",
        url=f"{_VOICE_URL}/{TURKISH_VOICE}.onnx",
        sha256="2844717f524ab965d3fe86e60562cbb601d3e456836efcc2196cc3a14112a8fb",
        size=63_201_294,
    ),
    PinnedFile(
        name=f"{TURKISH_VOICE}.onnx.json",
        url=f"{_VOICE_URL}/{TURKISH_VOICE}.onnx.json",
        sha256="13ebd7810f1b61b5027583cf3131a0a233b6ea81c38f2200ebc4ff41c3cca039",
        size=4_960,
    ),
    PinnedFile(
        # Kept beside the voice so that its licence travels with it.
        name=f"{TURKISH_VOICE}.MODEL_CARD",
        url=f"{_VOICE_URL}/MODEL_CARD",
        sha256="a3e08c57b9c6d428f8e4fb146512c8b1fb96a68c99d43820ed1dfea6d39cb35f",
        size=319,
    ),
)

LOCALE: Final = "tr"
PERSONAS: Final = ("sio", "zio")
"""Both are given the one voice, and the voice service raises it for Zio when her
model is the same file as his. Sio first, so that his name exists before hers
does and she is never heard speaking his voice unraised."""

ENGLISH_VOICE: Final = "en_US-ljspeech-medium"
"""The English corner's voice (D-225): one woman reading, US English, from the
LJ Speech recordings, which are in the public domain - so, unlike the Turkish
voice, nothing limits who may use it. Zio speaks it; she is the one who teaches."""

_ENGLISH_URL: Final = f"https://huggingface.co/rhasspy/piper-voices/resolve/{VOICES_COMMIT}/en/en_US/ljspeech/medium"

ENGLISH_FILES: Final = (
    PinnedFile(
        name=f"{ENGLISH_VOICE}.onnx",
        url=f"{_ENGLISH_URL}/{ENGLISH_VOICE}.onnx",
        sha256="6f52a751e2349abe7a76735eb09dc1875298c77ea2342ffd2fef79ff81b87f22",
        size=63_531_379,
    ),
    PinnedFile(
        name=f"{ENGLISH_VOICE}.onnx.json",
        url=f"{_ENGLISH_URL}/{ENGLISH_VOICE}.onnx.json",
        sha256="141d612cc0a95ed7efc1ca936b845c2364967f2e9217c5dbfcf69fc4d6c65860",
        size=4_972,
    ),
    PinnedFile(
        name=f"{ENGLISH_VOICE}.MODEL_CARD",
        url=f"{_ENGLISH_URL}/MODEL_CARD",
        sha256="fbee1529c89d36b3fe76d7e9f3f832dce17f44900a52d76a9bda735654766b4d",
        size=517,
    ),
)

ENGLISH_LOCALE: Final = "en"
ENGLISH_PERSONAS: Final = ("zio",)
"""Only Zio: a woman's voice for Sio would be a stranger, and Sio in English
keeps espeak-ng's man. The voice service raises no voice that only one persona
has, so hers is heard as it was recorded."""

DOWNLOAD_BYTES: Final = (
    PIPER_RELEASE.size + sum(f.size for f in VOICE_FILES) + sum(f.size for f in ENGLISH_FILES)
)
"""About 153 MB, which the wizard's box and the panel's button say before."""
ENGLISH_BYTES: Final = sum(f.size for f in ENGLISH_FILES)
"""About 64 MB: the English voice alone, for a family that has the Turkish one."""

MODEL_SUBDIR: Final = "piper"
"""Under `paths.model_dir`; where `VoiceBank` looks for `<locale>-<persona>.onnx`."""
MODEL_SUFFIX: Final = ".onnx"
CONFIG_SUFFIX: Final = ".json"
"""Piper reads a model's settings from the model's path plus this."""
PROGRAM: Final = "piper"
STAMP: Final = ".biokids-release"
"""In the unpacked Piper directory: the SHA-256 of the release it came from. Its
presence is also what lets `remove` delete the directory: nothing without it is
BioKids' to delete."""

SETUP_UNIT: Final = "biokids-voice-setup.service"

WANTED_SETTING: Final = "natural_voice"
"""The `setting` row that says the family asked for the natural voice (D-223):
`wanted` from the wizard's box, which is ticked for a Turkish family, or from
the button on /voices. While it says so and the voice is not on the machine,
the daemon starts the download again every `RETRY_SECONDS` - a machine set up
without a network gets it the first half hour it has one."""
WANTED: Final = "wanted"
RETRY_SECONDS: Final = 30 * 60
FIRST_TRY_AFTER_SECONDS: Final = 120
"""After the daemon starts: not during a boot, when the network may not be up."""

USER_AGENT: Final = "biokids-voice-setup"
CHUNK: Final = 1 << 20
TIMEOUT_S: Final = 60

INSTALLED: Final = "installed"
RUNNING: Final = "running"
FAILED: Final = "failed"
ABSENT: Final = "absent"

Opener = Callable[..., Any]
"""`urllib.request.urlopen`, injectable so the tests never touch the network."""


class VoiceSetupError(BioKidsError):
    """A download that will not be installed: wrong size, wrong checksum, bad archive."""


# ------------------------------------------------------------------- where


def voices_dir(config: Config) -> Path:
    return config.paths.model_dir / MODEL_SUBDIR


def persona_model(directory: Path, locale: str, persona: str) -> Path:
    """`<directory>/<locale>-<persona>.onnx`: the name `VoiceBank` looks for."""
    return directory / f"{locale}-{persona}{MODEL_SUFFIX}"


def model_config(model: Path) -> Path:
    return model.with_name(model.name + CONFIG_SUFFIX)


def _is_file(path: Path) -> bool:
    """`is_file()`, with "may not look" answered as "not there" (D-160)."""
    try:
        return path.is_file()
    except OSError:
        return False


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def find_piper(configured: str, directory: Path) -> str | None:
    """The Piper program to run: the downloaded one, else one on PATH.

    `configured` is `voice.piper_binary`. A path is used as it is. A bare name is
    looked for in `directory` first - `paths.piper_dir`, where the download puts
    it - and then on PATH, which is where a family who installed Piper by hand
    would have put theirs.
    """
    if "/" in configured or os.sep in configured:
        return configured if which(configured) else None
    downloaded = directory / configured
    if _is_file(downloaded) and os.access(downloaded, os.X_OK):
        return str(downloaded)
    return which(configured)


# ------------------------------------------------------------------- state


def piper_installed(directory: Path) -> bool:
    """The pinned release is unpacked in `directory`."""
    try:
        stamp = (directory / STAMP).read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return stamp == PIPER_RELEASE.sha256 and _is_file(directory / PROGRAM)


def voice_installed(directory: Path) -> bool:
    """Every voice file is there at its pinned size, and every persona has it.

    Sizes, not checksums: this runs on every load of the voices page, and each
    file's hash was checked when it arrived.
    """
    if any(_size(directory / f.name) != f.size for f in VOICE_FILES):
        return False
    return all(
        _is_file(persona_model(directory, LOCALE, persona))
        and _is_file(model_config(persona_model(directory, LOCALE, persona)))
        for persona in PERSONAS
    )


def english_installed(directory: Path) -> bool:
    """The English voice's files at their pinned sizes, under Zio's name (D-225)."""
    if any(_size(directory / f.name) != f.size for f in ENGLISH_FILES):
        return False
    return all(
        _is_file(persona_model(directory, ENGLISH_LOCALE, persona))
        and _is_file(model_config(persona_model(directory, ENGLISH_LOCALE, persona)))
        for persona in ENGLISH_PERSONAS
    )


def installed(config: Config) -> bool:
    directory = voices_dir(config)
    return (
        voice_installed(directory)
        and english_installed(directory)
        and piper_installed(config.paths.piper_dir)
    )


def english_speaks(config: Config) -> bool:
    """Would the English corner be read by Piper now, rather than espeak-ng?"""
    if find_piper(config.voice.piper_binary, config.paths.piper_dir) is None:
        return False
    directory = voices_dir(config)
    return all(_is_file(persona_model(directory, ENGLISH_LOCALE, p)) for p in ENGLISH_PERSONAS)


def natural_speaks(config: Config) -> bool:
    """Would Zio and Sio speak Turkish with Piper now, rather than espeak-ng?

    True after the download, and also where a family installed Piper and a voice
    by hand, which is why this does not ask `installed`.
    """
    if find_piper(config.voice.piper_binary, config.paths.piper_dir) is None:
        return False
    directory = voices_dir(config)
    return all(_is_file(persona_model(directory, LOCALE, p)) for p in PERSONAS)


def state(config: Config, runner: remote.CommandRunner | None = None) -> str:
    """`running` while the setup unit works, then `installed`, `failed` or `absent`.

    A oneshot unit is "activating" for as long as it runs and "inactive" once it
    has succeeded; systemd keeps "failed" until the next start, which is what
    lets the panel say so and offer the button again.
    """
    word = (runner or remote.run_command)(
        ("systemctl", "is-active", SETUP_UNIT), remote.DETECT_TIMEOUT
    ).stdout.strip()
    if word in {"activating", "active", "reloading", "deactivating"}:
        return RUNNING
    if installed(config):
        return INSTALLED
    return FAILED if word == "failed" else ABSENT


def progress(config: Config) -> int:
    """How much of the download is on the machine, in percent, for the panel.

    Read from the files themselves - finished ones at their full size, partial
    ones as far as they have got - so the setup writes no progress file of its own.
    """
    directory = voices_dir(config)
    done = 0
    for pinned in (*VOICE_FILES, *ENGLISH_FILES):
        final = directory / pinned.name
        done += pinned.size if _size(final) == pinned.size else _size(_partial(final))
    piper_dir = config.paths.piper_dir
    if piper_installed(piper_dir):
        done += PIPER_RELEASE.size
    else:
        done += _size(_partial(piper_dir.parent / PIPER_RELEASE.name))
    return min(100, done * 100 // DOWNLOAD_BYTES)


def describe(config: Config) -> dict[str, object]:
    """What `biokids-admin voice-setup status` prints."""
    return {
        "state": state(config),
        "natural_speaks": natural_speaks(config),
        "piper": find_piper(config.voice.piper_binary, config.paths.piper_dir),
        "voice": TURKISH_VOICE if voice_installed(voices_dir(config)) else None,
        "english": ENGLISH_VOICE if english_installed(voices_dir(config)) else None,
        "progress": progress(config),
    }


# ----------------------------------------------------------------- install


def _partial(final: Path) -> Path:
    return final.with_name(f".{final.name}.part")


def fetch(pinned: PinnedFile, directory: Path, *, opener: Opener | None = None) -> Path:
    """Download one pinned file beside where it will live, and check it.

    Returns the temporary file; the caller renames it into place. Anything larger
    than the pinned size is refused as it arrives, so a wrong answer from the
    server cannot fill the disk; anything else that does not match is deleted.
    """
    directory.mkdir(parents=True, exist_ok=True)
    partial = _partial(directory / pinned.name)
    # The URL is a constant in this file, reviewed with its checksum; nothing a
    # parent or a page sends ever reaches it.
    request = urllib.request.Request(pinned.url, headers={"User-Agent": USER_AGENT})  # noqa: S310
    digest = hashlib.sha256()
    received = 0
    try:
        with (
            (opener or urllib.request.urlopen)(request, timeout=TIMEOUT_S) as response,
            partial.open("wb") as out,
        ):
            while block := response.read(CHUNK):
                received += len(block)
                if received > pinned.size:
                    raise VoiceSetupError(f"{pinned.name}: over the pinned {pinned.size} bytes")
                digest.update(block)
                out.write(block)
        actual = digest.hexdigest()
        if received != pinned.size or actual != pinned.sha256:
            raise VoiceSetupError(
                f"{pinned.name}: {received} bytes with SHA-256 {actual}, "
                f"not the pinned {pinned.size} bytes with {pinned.sha256}"
            )
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    partial.chmod(0o644)
    return partial


def _link(path: Path, target: Path) -> None:
    """`path` becomes another name for `target`, atomically.

    A hard link rather than a symbolic one: the same file under the name the
    voice service looks for, no second copy of sixty megabytes, and nothing that
    needs a privilege to create on the Windows machine the tests also run on.
    """
    temporary = path.with_name(f".{path.name}.link")
    temporary.unlink(missing_ok=True)
    os.link(target, temporary)
    temporary.replace(path)


def _readable_by_all(root: Path) -> None:
    """Directories 0755, files 0644 or 0755: the voice service runs as the child.

    Not left to the umask. tarfile's `data` filter creates directories with
    whatever the umask allows, and a root shell with 0077 - a hardened machine,
    or someone running `biokids-admin voice-setup` by hand - would leave a Piper
    the child's session cannot open, which the voice service treats as no Piper
    at all (D-160).
    """
    for path in (root, *root.rglob("*")):
        if path.is_symlink():
            continue
        if path.is_dir():
            path.chmod(0o755)
        elif path.is_file():
            path.chmod(0o755 if path.stat().st_mode & 0o100 else 0o644)


def install_voice(directory: Path, *, opener: Opener | None = None) -> None:
    """The Turkish voice, and a name for it per persona."""
    _install_files(directory, VOICE_FILES, TURKISH_VOICE, LOCALE, PERSONAS, opener)


def install_english(directory: Path, *, opener: Opener | None = None) -> None:
    """The English corner's voice, under Zio's English name (D-225)."""
    _install_files(
        directory, ENGLISH_FILES, ENGLISH_VOICE, ENGLISH_LOCALE, ENGLISH_PERSONAS, opener
    )


def _install_files(
    directory: Path,
    files: Sequence[PinnedFile],
    voice: str,
    locale: str,
    personas: Sequence[str],
    opener: Opener | None,
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for made in (directory.parent, directory):
        made.chmod(0o755)
    fetched: list[tuple[PinnedFile, Path]] = []
    try:
        for pinned in files:
            if _size(directory / pinned.name) == pinned.size:
                continue  # checked when it arrived, by an earlier run
            log.info("downloading %s (%d kB)", pinned.name, pinned.size // 1024)
            fetched.append((pinned, fetch(pinned, directory, opener=opener)))
        # Every file is checked before any is moved: half a voice is not a voice.
        for pinned, partial in fetched:
            partial.replace(directory / pinned.name)
        fetched.clear()
    finally:
        for _pinned, partial in fetched:
            partial.unlink(missing_ok=True)

    model = directory / f"{voice}{MODEL_SUFFIX}"
    for persona in personas:
        named = persona_model(directory, locale, persona)
        # The settings first: the voice service looks for the model, and Piper
        # must find the settings beside it from the moment it does.
        _link(model_config(named), model_config(model))
        _link(named, model)


def install_piper(directory: Path, *, opener: Opener | None = None) -> None:
    """The Piper release, unpacked into `directory` (`paths.piper_dir`).

    Unpacked beside it and renamed into place, so the voice service sees either
    no Piper or a whole one. Only what is under the archive's own `piper/`
    directory is accepted, through tarfile's `data` filter: no absolute paths, no
    `..`, no link out of the directory, no device files, no setuid bits.
    """
    if piper_installed(directory):
        return
    parent = directory.parent
    log.info("downloading Piper %s (%d MB)", PIPER_VERSION, PIPER_RELEASE.size >> 20)
    tarball = fetch(PIPER_RELEASE, parent, opener=opener)
    staging = parent / f".{directory.name}.new"
    retired = parent / f".{directory.name}.old"
    try:
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir()
        try:
            with tarfile.open(tarball, "r:gz") as archive:
                members = archive.getmembers()
                for member in members:
                    if member.name != PROGRAM and not member.name.startswith(f"{PROGRAM}/"):
                        raise VoiceSetupError(f"not part of Piper: {member.name}")
                archive.extractall(staging, members=members, filter="data")
        except tarfile.TarError as exc:
            raise VoiceSetupError(f"the Piper archive could not be unpacked: {exc}") from exc
        unpacked = staging / PROGRAM
        program = unpacked / PROGRAM
        if not program.is_file():
            raise VoiceSetupError("the Piper archive has no piper program in it")
        program.chmod(0o755)
        (unpacked / STAMP).write_text(PIPER_RELEASE.sha256 + "\n", encoding="utf-8")
        _readable_by_all(unpacked)
        shutil.rmtree(retired, ignore_errors=True)
        if directory.exists():
            directory.rename(retired)
        try:
            unpacked.rename(directory)
        except OSError:
            if retired.exists() and not directory.exists():
                retired.rename(directory)
            raise
    finally:
        tarball.unlink(missing_ok=True)
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(retired, ignore_errors=True)


def install(config: Config, *, opener: Opener | None = None) -> None:
    """Everything, in the order that keeps what a child hears right throughout.

    Piper first, the voice last. The voice service caches audio by sentence and
    voice, never by engine, and a voice's key changes the moment its model
    appears (`biokids_voice.synth._fingerprint`). Were the model there before
    the program, espeak-ng would speak - and cache - lines under the new key,
    and those robot lines would be found again after Piper arrived. This way
    round, the first line spoken under the new key is spoken by Piper.
    """
    log.info(
        "%s: trained on a dataset licensed CC BY-NC-SA 4.0 - free for a family's "
        "personal, non-commercial use; downloaded from its authors, not shipped by BioKids",
        TURKISH_VOICE,
    )
    log.info(
        "%s: trained on the LJ Speech recordings, in the public domain (D-225)", ENGLISH_VOICE
    )
    install_piper(config.paths.piper_dir, opener=opener)
    install_voice(voices_dir(config), opener=opener)
    install_english(voices_dir(config), opener=opener)
    log.info("the natural voices are installed")


def remove(config: Config) -> None:
    """Delete what `install` put there, and nothing else."""
    directory = voices_dir(config)
    # Zio's name before Sio's, the reverse of `install_voice`, so that she is
    # never left speaking his voice unraised.
    for persona in reversed(PERSONAS):
        named = persona_model(directory, LOCALE, persona)
        for path in (named, model_config(named)):
            with contextlib.suppress(OSError):
                path.unlink()
    for persona in ENGLISH_PERSONAS:
        named = persona_model(directory, ENGLISH_LOCALE, persona)
        for path in (named, model_config(named)):
            with contextlib.suppress(OSError):
                path.unlink()
    for pinned in (*VOICE_FILES, *ENGLISH_FILES):
        for path in (directory / pinned.name, _partial(directory / pinned.name)):
            with contextlib.suppress(OSError):
                path.unlink()
    piper_dir = config.paths.piper_dir
    if (piper_dir / STAMP).is_file():
        shutil.rmtree(piper_dir, ignore_errors=True)
    with contextlib.suppress(OSError):
        _partial(piper_dir.parent / PIPER_RELEASE.name).unlink()
