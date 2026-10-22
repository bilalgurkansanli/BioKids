"""D-196: the natural voice, downloaded with one button.

Nothing here touches the network. The pinned files are swapped for small fakes
with checksums of their own, and `urlopen` for an opener that serves them, so
what is tested is exactly the code that runs on a family's machine: refusal of a
file that does not match, nothing half-installed, the names the voice service
looks for, and the Piper program found where the download put it.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import tarfile
import wave
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from biokids_core import admin, natural_voice, remote
from biokids_core.config import Config
from biokids_core.natural_voice import PinnedFile, VoiceSetupError
from biokids_voice import synth as synth_module
from biokids_voice.synth import (
    PIPER_LENGTH_SCALE,
    ZIO_LIFT,
    PiperSynth,
    StubSynth,
    Synthesizer,
    VoiceBank,
    VoiceCache,
    VoiceSpec,
    cache_key,
    write_silence,
)

ROOT = Path(__file__).resolve().parent.parent
BASE = "https://example.invalid/"


def pinned(name: str, data: bytes) -> PinnedFile:
    return PinnedFile(
        name=name, url=BASE + name, sha256=hashlib.sha256(data).hexdigest(), size=len(data)
    )


def piper_tarball(*extra: tarfile.TarInfo | tuple[str, bytes]) -> bytes:
    """A small stand-in for `piper_linux_x86_64.tar.gz`, laid out like it."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:

        def add(name: str, data: bytes | None = None, mode: int = 0o644) -> None:
            info = tarfile.TarInfo(name)
            if data is None:
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                archive.addfile(info)
            else:
                info.size = len(data)
                info.mode = mode
                archive.addfile(info, io.BytesIO(data))

        add("piper")
        add("piper/piper", b"\x7fELF a program", 0o755)
        add("piper/libpiper_phonemize.so.1.2.0", b"a library")
        link = tarfile.TarInfo("piper/libpiper_phonemize.so.1")
        link.type = tarfile.SYMTYPE
        link.linkname = "libpiper_phonemize.so.1.2.0"
        archive.addfile(link)
        add("piper/espeak-ng-data")
        add("piper/espeak-ng-data/tr_dict", b"turkish")
        for item in extra:
            if isinstance(item, tarfile.TarInfo):
                archive.addfile(item)
            else:
                add(*item)
    return buffer.getvalue()


class Server:
    """Serves the fake release; remembers what was asked for."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.requested: list[str] = []

    def __call__(self, request: Any, timeout: float | None = None) -> io.BytesIO:
        self.requested.append(request.full_url)
        return io.BytesIO(self.files[request.full_url])

    def serve(self, entry: PinnedFile, data: bytes) -> None:
        self.files[entry.url] = data


MODEL = b"an onnx model, " * 64
SETTINGS = b'{"audio": {"sample_rate": 22050}, "dataset": "dfki"}'
CARD = b"# Model card for dfki (medium)\n"
ENGLISH_MODEL = b"an english onnx model, " * 48
ENGLISH_SETTINGS = b'{"audio": {"sample_rate": 22050}, "dataset": "ljspeech"}'
ENGLISH_CARD = b"# Model card for ljspeech (medium)\n"


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Server:
    """The release BioKids pins, replaced by fakes pinned the same way."""
    served = Server()
    files = (
        (pinned(f"{natural_voice.TURKISH_VOICE}.onnx", MODEL), MODEL),
        (pinned(f"{natural_voice.TURKISH_VOICE}.onnx.json", SETTINGS), SETTINGS),
        (pinned(f"{natural_voice.TURKISH_VOICE}.MODEL_CARD", CARD), CARD),
    )
    english = (
        (pinned(f"{natural_voice.ENGLISH_VOICE}.onnx", ENGLISH_MODEL), ENGLISH_MODEL),
        (
            pinned(f"{natural_voice.ENGLISH_VOICE}.onnx.json", ENGLISH_SETTINGS),
            ENGLISH_SETTINGS,
        ),
        (pinned(f"{natural_voice.ENGLISH_VOICE}.MODEL_CARD", ENGLISH_CARD), ENGLISH_CARD),
    )
    tarball = piper_tarball()
    release = pinned(natural_voice.PIPER_RELEASE.name, tarball)
    for entry, data in (*files, *english):
        served.serve(entry, data)
    served.serve(release, tarball)
    monkeypatch.setattr(natural_voice, "VOICE_FILES", tuple(entry for entry, _ in files))
    monkeypatch.setattr(natural_voice, "ENGLISH_FILES", tuple(entry for entry, _ in english))
    monkeypatch.setattr(natural_voice, "PIPER_RELEASE", release)
    monkeypatch.setattr(
        natural_voice,
        "DOWNLOAD_BYTES",
        release.size + sum(len(d) for _, d in (*files, *english)),
    )
    return served


def voices(config: Config) -> Path:
    return natural_voice.voices_dir(config)


def leftovers(directory: Path) -> list[str]:
    """Temporary names the setup uses, which must never outlive it."""
    if not directory.exists():
        return []
    return sorted(p.name for p in directory.iterdir() if p.name.startswith("."))


# ------------------------------------------------------- the real pins


def test_what_is_pinned_is_what_was_checked() -> None:
    """The URLs name a release and a commit, never a branch that moves."""
    release = natural_voice.PIPER_RELEASE
    assert "/releases/download/2023.11.14-2/" in release.url
    assert release.url.startswith("https://github.com/rhasspy/piper/")
    for entry in (*natural_voice.VOICE_FILES, *natural_voice.ENGLISH_FILES):
        assert entry.url.startswith("https://huggingface.co/rhasspy/piper-voices/resolve/")
        assert f"/resolve/{natural_voice.VOICES_COMMIT}/" in entry.url
        assert "/main/" not in entry.url
    for entry in (release, *natural_voice.VOICE_FILES, *natural_voice.ENGLISH_FILES):
        assert len(entry.sha256) == 64 and int(entry.sha256, 16) >= 0
        assert entry.size > 0
    # The box and the button say "about 153 MB"; this is where that number comes from.
    assert round(natural_voice.DOWNLOAD_BYTES / 1_000_000) == 153
    assert round(natural_voice.ENGLISH_BYTES / 1_000_000) == 64
    assert "/en/en_US/ljspeech/medium/" in natural_voice.ENGLISH_FILES[0].url


# ------------------------------------------------------------- fetching


def test_a_file_that_does_not_match_its_checksum_is_refused(
    tmp_path: Path, server: Server
) -> None:
    entry = natural_voice.VOICE_FILES[0]
    server.serve(entry, MODEL[:-1] + b"!")  # the same size, one byte different

    with pytest.raises(VoiceSetupError, match="SHA-256"):
        natural_voice.fetch(entry, tmp_path, opener=server)

    assert list(tmp_path.iterdir()) == [], "a file that failed its check must not be kept"


def test_a_file_larger_than_pinned_is_cut_off_as_it_arrives(
    tmp_path: Path, server: Server
) -> None:
    entry = natural_voice.VOICE_FILES[0]
    server.serve(entry, MODEL + b"and then some")

    with pytest.raises(VoiceSetupError, match="over the pinned"):
        natural_voice.fetch(entry, tmp_path, opener=server)
    assert list(tmp_path.iterdir()) == []


def test_a_broken_connection_leaves_nothing_behind(tmp_path: Path, server: Server) -> None:
    def refuse(_request: Any, timeout: float | None = None) -> io.BytesIO:
        raise OSError("connection reset")

    with pytest.raises(OSError):
        natural_voice.fetch(natural_voice.VOICE_FILES[0], tmp_path, opener=refuse)
    assert list(tmp_path.iterdir()) == []


# ------------------------------------------------------------ the voice


def test_half_a_voice_is_never_installed(config: Config, server: Server) -> None:
    """The model arrives and checks out; its settings do not. Neither is used."""
    server.serve(natural_voice.VOICE_FILES[1], b'{"tampered": true}')

    with pytest.raises(VoiceSetupError):
        natural_voice.install_voice(voices(config), opener=server)

    directory = voices(config)
    assert not (directory / f"{natural_voice.TURKISH_VOICE}.onnx").exists()
    assert not natural_voice.persona_model(directory, "tr", "sio").exists()
    assert leftovers(directory) == []
    assert VoiceBank(config).stock_model("sio", "tr") is None


def test_each_persona_gets_the_voice_under_the_name_the_voice_service_looks_for(
    config: Config, server: Server
) -> None:
    natural_voice.install_voice(voices(config), opener=server)

    bank = VoiceBank(config)
    for persona in ("zio", "sio"):
        model = bank.stock_model(persona, "tr")
        assert model is not None and model.name == f"tr-{persona}.onnx"
        assert model.read_bytes() == MODEL
        # Piper reads the model's settings from its own path plus ".json".
        assert model.with_name(model.name + ".json").read_bytes() == SETTINGS
    assert (voices(config) / f"{natural_voice.TURKISH_VOICE}.MODEL_CARD").read_bytes() == CARD
    assert bank.stock_model("zio", "en") is None, "only Turkish has a natural voice"
    assert natural_voice.voice_installed(voices(config))
    assert leftovers(voices(config)) == []


def test_a_second_run_downloads_nothing_already_there(config: Config, server: Server) -> None:
    natural_voice.install_voice(voices(config), opener=server)
    asked = len(server.requested)

    natural_voice.install_voice(voices(config), opener=server)

    assert len(server.requested) == asked


# ---------------------------------------------------------------- Piper


def test_piper_is_unpacked_where_the_voice_service_finds_it(
    config: Config, server: Server
) -> None:
    piper_dir = config.paths.piper_dir
    natural_voice.install_piper(piper_dir, opener=server)

    program = piper_dir / "piper"
    assert program.read_bytes() == b"\x7fELF a program"
    assert (piper_dir / "espeak-ng-data" / "tr_dict").is_file(), "Piper needs its phonemes"
    assert (piper_dir / "libpiper_phonemize.so.1").exists()
    assert natural_voice.piper_installed(piper_dir)
    assert natural_voice.find_piper("piper", piper_dir) == str(program)
    assert leftovers(piper_dir.parent) == [], "the archive and the staging directory are gone"


@pytest.mark.parametrize(
    "intruder",
    [
        ("outside.txt", b"not under piper/"),
        ("piper/../../escaped", b"climbs out"),
    ],
)
def test_an_archive_reaching_outside_piper_is_refused(
    config: Config, server: Server, monkeypatch: pytest.MonkeyPatch, intruder: tuple[str, bytes]
) -> None:
    tarball = piper_tarball(intruder)
    release = pinned(natural_voice.PIPER_RELEASE.name, tarball)
    server.serve(release, tarball)
    monkeypatch.setattr(natural_voice, "PIPER_RELEASE", release)

    with pytest.raises(VoiceSetupError):
        natural_voice.install_piper(config.paths.piper_dir, opener=server)

    assert not config.paths.piper_dir.exists()
    assert not (config.paths.piper_dir.parent / "escaped").exists()
    assert leftovers(config.paths.piper_dir.parent) == []


def test_a_link_out_of_the_archive_is_refused(
    config: Config, server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    link = tarfile.TarInfo("piper/passwd")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    tarball = piper_tarball(link)
    release = pinned(natural_voice.PIPER_RELEASE.name, tarball)
    server.serve(release, tarball)
    monkeypatch.setattr(natural_voice, "PIPER_RELEASE", release)

    with pytest.raises(VoiceSetupError):
        natural_voice.install_piper(config.paths.piper_dir, opener=server)
    assert not config.paths.piper_dir.exists()


def test_the_whole_setup_puts_piper_in_place_before_the_voice(
    config: Config, server: Server
) -> None:
    """Piper first: lines cached under the voice's new key are Piper's (D-196)."""
    natural_voice.install(config, opener=server)

    order = [url.rsplit("/", 1)[1] for url in server.requested]
    assert order[0] == natural_voice.PIPER_RELEASE.name
    assert natural_voice.installed(config)
    assert natural_voice.natural_speaks(config)


# ---------------------------------------------------------------- state


def runner_saying(word: str) -> remote.CommandRunner:
    def run(argv: Any, timeout: float, /) -> remote.CommandResult:
        assert list(argv) == ["systemctl", "is-active", natural_voice.SETUP_UNIT]
        return remote.CommandResult(stdout=word + "\n")

    return run


def test_the_state_the_panel_shows(config: Config, server: Server) -> None:
    assert natural_voice.state(config, runner_saying("inactive")) == natural_voice.ABSENT
    assert natural_voice.state(config, runner_saying("failed")) == natural_voice.FAILED
    assert natural_voice.state(config, runner_saying("activating")) == natural_voice.RUNNING

    natural_voice.install(config, opener=server)

    assert natural_voice.state(config, runner_saying("inactive")) == natural_voice.INSTALLED
    assert natural_voice.state(config, runner_saying("failed")) == natural_voice.INSTALLED


def test_progress_is_read_from_the_files_themselves(config: Config, server: Server) -> None:
    assert natural_voice.progress(config) == 0

    natural_voice.install_piper(config.paths.piper_dir, opener=server)
    model = natural_voice.VOICE_FILES[0]
    partial = voices(config) / f".{model.name}.part"
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_bytes(MODEL[: len(MODEL) // 2])

    done = natural_voice.PIPER_RELEASE.size + len(MODEL) // 2
    assert natural_voice.progress(config) == done * 100 // natural_voice.DOWNLOAD_BYTES

    partial.unlink()
    natural_voice.install_voice(voices(config), opener=server)
    assert natural_voice.progress(config) < 100, "the English voice is still to come"
    natural_voice.install_english(voices(config), opener=server)
    assert natural_voice.progress(config) == 100


def test_remove_takes_away_what_the_download_put_there_and_nothing_else(
    config: Config, server: Server
) -> None:
    natural_voice.install(config, opener=server)
    recording = config.paths.recorded_voice_dir / "tr" / "warn_1.wav"
    recording.parent.mkdir(parents=True, exist_ok=True)
    recording.write_bytes(b"a parent's voice")
    own = voices(config) / "en-sio.onnx"
    own.write_bytes(b"a voice the family added by hand")

    natural_voice.remove(config)

    assert not config.paths.piper_dir.exists()
    assert VoiceBank(config).stock_model("sio", "tr") is None
    assert VoiceBank(config).stock_model("zio", "en") is None, "the English voice too"
    assert recording.exists(), "the parent's recordings are not the download's"
    assert own.exists()


def test_remove_never_deletes_a_piper_directory_it_did_not_make(config: Config) -> None:
    piper_dir = config.paths.piper_dir
    piper_dir.mkdir(parents=True)
    (piper_dir / "piper").write_bytes(b"installed by hand")

    natural_voice.remove(config)

    assert (piper_dir / "piper").exists()


# --------------------------------------------------- the voice service


def test_the_downloaded_piper_is_found_before_the_one_on_path(
    config: Config, server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(natural_voice, "which", lambda name: f"/usr/bin/{name}")
    synth = PiperSynth("piper", directory=config.paths.piper_dir)
    assert synth.program() == "/usr/bin/piper", "nothing downloaded: PATH is the fallback"

    natural_voice.install_piper(config.paths.piper_dir, opener=server)

    assert synth.program() == str(config.paths.piper_dir / "piper")
    assert PiperSynth("/opt/piper/piper", directory=config.paths.piper_dir).program() == (
        "/opt/piper/piper"
    ), "a path in config.toml is used as it is"


def test_with_no_piper_anywhere_it_is_not_available(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(natural_voice, "which", lambda _name: None)
    assert PiperSynth("piper", directory=config.paths.piper_dir).available() is False


def test_zio_speaks_sios_voice_higher_and_sio_as_it_is(config: Config, server: Server) -> None:
    natural_voice.install_voice(voices(config), opener=server)
    bank = VoiceBank(config)

    (zio,) = bank.candidates("zio", "tr")
    (sio,) = bank.candidates("sio", "tr")

    assert zio.lift == ZIO_LIFT and sio.lift == 1.0
    assert zio.profile_id.endswith(f":lift {ZIO_LIFT:g}")
    # Changing the lift must be heard at once, not after the cache empties.
    assert cache_key("Bir dakika kaldı.", zio) != cache_key(
        "Bir dakika kaldı.", replace(zio, profile_id=zio.profile_id.rsplit(":", 1)[0])
    )


def test_a_voice_of_zios_own_is_not_raised(config: Config, server: Server) -> None:
    natural_voice.install_voice(voices(config), opener=server)
    hers = natural_voice.persona_model(voices(config), "tr", "zio")
    hers.unlink()
    hers.write_bytes(b"a woman's voice a family found")

    (zio,) = VoiceBank(config).candidates("zio", "tr")

    assert zio.lift == 1.0


class FakePiper:
    """Stands in for `subprocess.run(["piper", ...])`: writes a WAV, keeps the argv."""

    def __init__(self) -> None:
        self.argv: list[str] = []

    def __call__(self, argv: list[str], **_kwargs: Any) -> Any:
        self.argv = argv
        write_silence(Path(argv[argv.index("--output_file") + 1]), 0.5, 22_050)

        class Finished:
            returncode = 0
            stdout = b""
            stderr = b""

        return Finished()


def rate_of(path: Path) -> int:
    with wave.open(str(path), "rb") as handle:
        return handle.getframerate()


def test_piper_is_slowed_then_lifted_for_zio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slower by the lift, played faster by the lift: higher, at the same pace."""
    fake = FakePiper()
    monkeypatch.setattr(synth_module.subprocess, "run", fake)
    downloaded = "/var/lib/biokids-voices/piper/piper"
    monkeypatch.setattr(PiperSynth, "program", lambda _self: downloaded)
    model = tmp_path / "tr-zio.onnx"
    model.write_bytes(b"model")
    synth = PiperSynth()

    sio = VoiceSpec(persona="sio", locale="tr", profile_id="s", model_path=model)
    synth.render("Merhaba.", sio, tmp_path / "sio.wav")
    assert fake.argv[0] == downloaded
    assert fake.argv[fake.argv.index("--length_scale") + 1] == f"{PIPER_LENGTH_SCALE:.3f}"
    assert rate_of(tmp_path / "sio.wav") == 22_050

    zio = VoiceSpec(persona="zio", locale="tr", profile_id="z", model_path=model, lift=ZIO_LIFT)
    synth.render("Merhaba.", zio, tmp_path / "zio.wav")
    scale = float(fake.argv[fake.argv.index("--length_scale") + 1])
    assert scale == pytest.approx(PIPER_LENGTH_SCALE * ZIO_LIFT, abs=1e-3)
    assert rate_of(tmp_path / "zio.wav") == round(22_050 * ZIO_LIFT)


class LatePiper(StubSynth):
    """An engine that is not installed yet, until it is."""

    name = "piper"

    def __init__(self) -> None:
        super().__init__()
        self.installed = False

    def available(self) -> bool:
        return self.installed


def test_a_piper_downloaded_while_the_child_is_at_the_machine_speaks_the_next_line(
    tmp_path: Path,
) -> None:
    """Without this, the child heard the robot until their next login."""
    late = LatePiper()
    robot = StubSynth()
    chain = Synthesizer(
        [(robot, VoiceCache(tmp_path / "espeak", 1 << 20))],
        standby=[(late, VoiceCache(tmp_path, 1 << 20))],
    )
    voice = VoiceSpec(persona="sio", locale="tr", profile_id="stock:sio:tr:absent")

    chain.render("Beş dakika kaldı.", [voice])
    assert chain.backend is robot

    late.installed = True
    chain.render("Bir dakika kaldı.", [voice])

    assert chain.backend is late
    assert chain.engines[0][0] is late


def test_the_piper_cache_is_emptied_when_piper_is_tuned_differently(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(PiperSynth, "available", lambda _self: True)
    old = VoiceCache.from_config(config)
    old.directory.mkdir(parents=True, exist_ok=True)
    stale = old.path_for("before")
    stale.write_bytes(b"audio from another tuning")

    Synthesizer.from_config(config)

    assert not stale.exists()


# ------------------------------------------------------------ the command


def test_biokids_admin_voice_setup_says_when_it_failed(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_config: Config, **_kwargs: Any) -> None:
        raise VoiceSetupError("checksum does not match")

    monkeypatch.setattr(natural_voice, "install", broken)
    assert admin.cmd_voice_setup(argparse.Namespace(action="download"), config) == 1


def test_biokids_admin_voice_setup_status_is_json(
    config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    assert admin.cmd_voice_setup(argparse.Namespace(action="status"), config) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["state"] == natural_voice.ABSENT
    assert shown["natural_speaks"] is False


def test_the_command_line_knows_the_subcommand() -> None:
    args = admin.build_parser().parse_args(["voice-setup"])
    assert args.command == "voice-setup" and args.action == "download"


# ------------------------------------------------------------- packaging


def unit_text() -> str:
    return (ROOT / "debian" / f"biokids-voice.{natural_voice.SETUP_UNIT}").read_text(
        encoding="utf-8"
    )


def test_the_setup_unit_is_a_hardened_oneshot_nobody_enables() -> None:
    text = unit_text()
    assert "Type=oneshot" in text
    assert "[Install]" not in text, "only the daemon starts it, for a parent who asked"
    assert "ExecStart=/usr/bin/biokids-admin voice-setup download" in text
    for line in (
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateTmp=yes",
        "CapabilityBoundingSet=",
        "ReadWritePaths=/var/lib/biokids-voices",
    ):
        assert f"\n{line}\n" in text, line
    rules = (ROOT / "debian" / "rules").read_text(encoding="utf-8")
    assert (
        "dh_installsystemd -pbiokids-voice --name=biokids-voice-setup --no-enable --no-start"
    ) in rules


def test_the_download_lands_where_the_unit_may_write() -> None:
    """ReadWritePaths= is the voices directory; both destinations are inside it."""
    defaults = Config().paths
    root = Path("/var/lib/biokids-voices")
    assert root in defaults.piper_dir.parents
    assert root in defaults.model_dir.parents


def test_the_english_voice_is_zios_and_heard_as_it_was_recorded(
    config: Config, server: Server
) -> None:
    """D-225: one woman's voice, for Zio, the one who teaches; Sio keeps espeak-ng."""
    natural_voice.install(config, opener=server)
    bank = VoiceBank(config)
    model = bank.stock_model("zio", "en")
    assert model is not None and model.read_bytes() == ENGLISH_MODEL
    assert bank.stock_model("sio", "en") is None
    assert bank.lift("zio", "en", model) == 1.0, "a voice only Zio has is not raised"
    assert natural_voice.installed(config)
    assert natural_voice.describe(config)["english"] == natural_voice.ENGLISH_VOICE


def test_a_family_with_the_turkish_voice_gets_only_the_english_one(
    config: Config, server: Server
) -> None:
    natural_voice.install_piper(config.paths.piper_dir, opener=server)
    natural_voice.install_voice(voices(config), opener=server)
    assert not natural_voice.installed(config), "not until the English voice is there"
    server.requested.clear()
    natural_voice.install(config, opener=server)
    assert server.requested and all(
        natural_voice.ENGLISH_VOICE in url for url in server.requested
    ), server.requested
    assert natural_voice.installed(config)
