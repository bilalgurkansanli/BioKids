"""Machine-level configuration: `/etc/biokids/config.toml`.

Every path and port from `CLAUDE.md` §5 lives here rather than being written as a
literal somewhere in the code. That is what makes it possible to run the whole
system out of a scratch directory during development, and what keeps the Debian
packaging honest about which files it owns.

Validation is deliberately hand-written instead of pulling in pydantic:
`biokids-core` is installed on every machine that runs any part of BioKids, and
its dependency list is `python3`, `python3-sqlalchemy` and `alembic`.
"""

from __future__ import annotations

import logging
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Final, Self, get_args, get_origin, get_type_hints

from biokids_core.errors import ConfigError

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH: Final = Path("/etc/biokids/config.toml")
ENV_CONFIG_PATH: Final = "BIOKIDS_CONFIG"
ENV_PREFIX_DIR: Final = "BIOKIDS_PREFIX"
"""Points every absolute path at a scratch root. Used by tests and `dev/reset.sh`."""


@dataclass(frozen=True, slots=True)
class PathsConfig:
    config_dir: Path = Path("/etc/biokids")
    data_dir: Path = Path("/var/lib/biokids")
    db_path: Path = Path("/var/lib/biokids/biokids.db")
    voice_cache_dir: Path = Path("/var/lib/biokids/voice-cache")
    model_dir: Path = Path("/var/lib/biokids-voices/models")
    """Piper voices a family installed themselves (D-137), under `piper/`. In the
    world-readable voices directory because the voice service runs as the child."""
    piper_dir: Path = Path("/var/lib/biokids-voices/piper")
    """The Piper program the panel's "natural voice" button downloads (D-196).
    Looked in before PATH; world-readable for the same reason."""
    run_dir: Path = Path("/run/biokids")
    daemon_socket: Path = Path("/run/biokids/daemon.sock")
    shell_socket: Path = Path("/run/biokids/shell.sock")
    """The child launcher's socket. World-connectable by necessity — the child
    is not in the `biokids` group — and authorised by the peer's uid instead,
    which the kernel supplies and nothing in a child's session can forge."""
    share_dir: Path = Path("/usr/share/biokids")
    secret_key: Path = Path("/etc/biokids/secret.key")
    chromium_policy_dir: Path = Path("/etc/chromium/policies/managed")
    extension_dir: Path = Path("/var/lib/biokids-ext")
    """The packed browser extension, its update manifest and its id (D-187).
    World-readable: the child's Chromium installs it from here."""
    extension_key: Path = Path("/var/lib/biokids/extension-key.pem")
    """The key the extension is signed with, made on this machine, never shipped."""
    ai_run_dir: Path = Path("/run/biokids-ai")
    """The assistant service's own runtime directory: its socket, and the socket
    of the language model it runs (D-165)."""
    ai_models_dir: Path = Path("/var/lib/biokids-ai/models")
    """Model files a parent downloaded for the assistant, owned by `biokids-ai`."""
    recorded_voice_dir: Path = Path("/var/lib/biokids-voices")
    """A parent's own recordings of the spoken lines (D-174). Outside the 0700
    data directory because the voice service, which runs as the child, plays them."""

    @property
    def catalog_dir(self) -> Path:
        return self.share_dir / "catalog"

    @property
    def voice_lines_dir(self) -> Path:
        return self.share_dir / "voice"


@dataclass(frozen=True, slots=True)
class PanelConfig:
    host: str = "127.0.0.1"
    port: int = 7878
    session_idle_minutes: int = 30
    pin_attempts: int = 5
    pin_lockout_minutes: int = 15
    """First lockout. Doubles on each subsequent trip, capped at a day (D-48)."""
    pin_lockout_max_minutes: int = 1440


@dataclass(frozen=True, slots=True)
class ShellConfig:
    host: str = "127.0.0.1"
    port: int = 7879
    """The first launcher port. Each child's launcher listens on
    `port + uid % 100` (`launcher_port`), so two children logged in at once do
    not fight over one port (D-188)."""
    kiosk_command: str = "chromium"
    kiosk_restart_seconds: int = 2

    def launcher_port(self, uid: int) -> int:
        return self.port + uid % LAUNCHER_PORTS

    def launcher_origin(self, uid: int) -> str:
        return f"{self.host}:{self.launcher_port(uid)}"


@dataclass(frozen=True, slots=True)
class DaemonConfig:
    tick_seconds: int = 10
    persist_seconds: int = 30
    clock_jump_threshold_seconds: int = 600
    socket_group: str = "biokids"
    socket_mode: int = 0o660
    lock_countdown_seconds: int = 120
    app_term_grace_seconds: int = 10
    """SIGTERM, then SIGKILL after this long, so a child's drawing gets saved."""


@dataclass(frozen=True, slots=True)
class VoiceConfig:
    enabled: bool = True
    socket_name: str = "biokids-voice.sock"
    cache_max_mb: int = 200
    queue_depth: int = 3
    duck_percent: int = 30
    piper_binary: str = "piper"
    """A bare name is looked for in `paths.piper_dir` first, where the panel's
    "natural voice" button puts it (D-196), then on PATH; a path is used as is."""
    espeak_binary: str = "espeak-ng"
    """The fallback engine, tried when Piper is absent. It usually is: piper-tts
    is not in Debian 13 and the last Turkish Piper voice is CC-BY-NC-SA."""
    mbrola_binary: str = "mbrola"
    """Used only when a family has installed the MBROLA Turkish voices (D-161)."""
    quiet_hours: str = ""
    """Empty means never quiet. Otherwise a window like "21:00-08:00"."""


@dataclass(frozen=True, slots=True)
class AiConfig:
    assistant_per_10min: int = 20
    """Questions a child may ask the assistant in ten minutes (D-165)."""
    llama_server_binary: str = "llama-server"
    whisper_server_binary: str = "whisper-server"
    kiwix_serve_binary: str = "kiwix-serve"
    """Searches the encyclopedia the assistant checks its answers against (D-178)."""
    idle_unload_minutes: int = 10
    """A model nobody has asked anything for this long is unloaded, giving its
    memory back to the child's apps."""
    threads: int = 0
    """CPU threads for the models; 0 means half the logical CPUs, at most 8."""


@dataclass(frozen=True, slots=True)
class RetentionConfig:
    """How long the family's records are kept (ARCHITECTURE.md section 5, D-189).

    Nothing is kept forever. What a child asked the assistant and which sites
    they visited are the most personal and go first."""

    activity_days: int = 30
    """Usage events (sites visited, gaps in the ledger) and the assistant's alerts."""
    usage_days: int = 400
    """Minutes per day, per app and per hour of the day, for the history and
    insights screens and their year."""
    audit_days: int = 365
    requests_days: int = 90


@dataclass(frozen=True, slots=True)
class I18nConfig:
    default_locale: str = "tr"
    available: tuple[str, ...] = ("tr", "en")


@dataclass(frozen=True, slots=True)
class Config:
    paths: PathsConfig = field(default_factory=PathsConfig)
    panel: PanelConfig = field(default_factory=PanelConfig)
    shell: ShellConfig = field(default_factory=ShellConfig)
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    ai: AiConfig = field(default_factory=AiConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    i18n: I18nConfig = field(default_factory=I18nConfig)

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.paths.db_path}"

    # -------------------------------------------------------------- loading

    @classmethod
    def load(cls, path: Path | str | None = None) -> Self:
        """Read the config file, or return defaults when it does not exist yet.

        A missing file is normal: the package ships one, but the daemon must also
        start on a machine where an admin deleted it. A *malformed* file is not
        normal and raises, because guessing at a parental-control config is worse
        than refusing to start.
        """
        chosen = _resolve_path(path)
        data: Mapping[str, Any] = {}
        if chosen is not None and chosen.is_file():
            try:
                data = tomllib.loads(chosen.read_text(encoding="utf-8"))
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError(f"{chosen}: not valid TOML: {exc}") from exc
            except OSError as exc:
                raise ConfigError(f"{chosen}: cannot be read: {exc}") from exc

        config = cls.from_mapping(data)
        prefix = os.environ.get(ENV_PREFIX_DIR)
        return config.with_prefix(Path(prefix)) if prefix else config

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Self:
        hints = _hints(cls)
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ConfigError(
                f"unknown config section(s): {', '.join(unknown)}. "
                f"Known sections: {', '.join(sorted(known))}"
            )
        sections: dict[str, Any] = {}
        for spec in fields(cls):
            section_type = hints[spec.name]
            raw = data.get(spec.name, {})
            if not isinstance(raw, Mapping):
                raise ConfigError(f"[{spec.name}] must be a table, got {type(raw).__name__}")
            sections[spec.name] = _build_section(spec.name, section_type, raw)
        return cls(**sections)

    def with_prefix(self, prefix: Path) -> Self:
        """Re-root every absolute path under `prefix`.

        `BIOKIDS_PREFIX=/tmp/bk` turns `/var/lib/biokids/biokids.db` into
        `/tmp/bk/var/lib/biokids/biokids.db`. This is the single mechanism that
        lets the panel, the daemon and the test suite run unprivileged on a
        developer machine without a fork of the path layout.
        """
        rerooted = {
            spec.name: _reroot(prefix, getattr(self.paths, spec.name))
            for spec in fields(PathsConfig)
        }
        kwargs: dict[str, Any] = {spec.name: getattr(self, spec.name) for spec in fields(self)}
        kwargs["paths"] = PathsConfig(**rerooted)
        return type(self)(**kwargs)


def _reroot(prefix: Path, path: Path) -> Path:
    """Join `path` under `prefix`, dropping whatever root `path` carries.

    `Path.is_absolute()` is not usable here: on a Windows development host a
    POSIX path like `/var/lib/biokids` has a root but no drive, so it reports
    False, and `prefix / path` would then silently discard the prefix. Stripping
    the anchor by hand behaves the same on both platforms.
    """
    anchor = path.anchor
    parts = [p for p in path.parts if p not in {"/", "\\", anchor}]
    return prefix.joinpath(*parts) if parts else prefix


def _resolve_path(path: Path | str | None) -> Path | None:
    if path is not None:
        return Path(path)
    from_env = os.environ.get(ENV_CONFIG_PATH)
    if from_env:
        return Path(from_env)
    prefix = os.environ.get(ENV_PREFIX_DIR)
    if prefix:
        return _reroot(Path(prefix), DEFAULT_CONFIG_PATH)
    return DEFAULT_CONFIG_PATH


def _hints(cls: type) -> dict[str, Any]:
    """Resolve a dataclass's annotations.

    `from __future__ import annotations` turns every field type into a string, so
    the loader has to ask typing for the real objects. Cached, because the panel
    reloads the config on every settings change.
    """
    cached = _HINT_CACHE.get(cls)
    if cached is None:
        cached = get_type_hints(cls)
        _HINT_CACHE[cls] = cached
    return cached


_HINT_CACHE: dict[type, dict[str, Any]] = {}


def _build_section(section: str, section_type: Any, raw: Mapping[str, Any]) -> Any:
    if not (isinstance(section_type, type) and is_dataclass(section_type)):
        raise ConfigError(f"config section {section} is not a dataclass")
    hints = _hints(section_type)
    known = {f.name for f in fields(section_type)}
    retired = sorted(set(raw) & RETIRED_KEYS.get(section, frozenset()))
    if retired:
        # An edited config.toml is a conffile dpkg keeps across upgrades, so a
        # key a later version dropped must not stop the daemon - which would
        # lock every child out. Said, and ignored.
        log.warning("[%s]: ignoring retired key(s): %s", section, ", ".join(retired))
        raw = {key: value for key, value in raw.items() if key not in retired}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ConfigError(
            f"[{section}]: unknown key(s): {', '.join(unknown)}. "
            f"Known keys: {', '.join(sorted(known))}"
        )
    kwargs: dict[str, Any] = {}
    for key, value in raw.items():
        kwargs[key] = _coerce(f"{section}.{key}", hints[key], value)
    try:
        return section_type(**kwargs)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"[{section}]: {exc}") from exc


def _coerce(where: str, expected: Any, value: Any) -> Any:
    """Convert a TOML scalar to the field's declared type, or explain why not."""
    origin = get_origin(expected)
    if origin is tuple:
        if not isinstance(value, list):
            raise ConfigError(f"{where}: expected a list, got {type(value).__name__}")
        member = get_args(expected)[0]
        return tuple(_coerce(where, member, item) for item in value)

    if expected is Path:
        if not isinstance(value, str):
            raise ConfigError(f"{where}: expected a path string, got {type(value).__name__}")
        if not value.strip():
            raise ConfigError(f"{where}: path may not be empty")
        return Path(value)

    if expected is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"{where}: expected true or false, got {value!r}")
        return value

    if expected is int:
        # bool is a subclass of int; a boolean where a number belongs is a typo.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{where}: expected a whole number, got {value!r}")
        if value < 0:
            raise ConfigError(f"{where}: may not be negative, got {value}")
        return value

    if expected is float:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ConfigError(f"{where}: expected a number, got {value!r}")
        return float(value)

    if expected is str:
        if not isinstance(value, str):
            raise ConfigError(f"{where}: expected a string, got {type(value).__name__}")
        return value

    raise ConfigError(f"{where}: unsupported config type {expected!r}")  # pragma: no cover


LAUNCHER_PORTS: Final = 100
"""Launcher ports run from `shell.port` to `shell.port + 99`."""

RETIRED_KEYS: Final[dict[str, frozenset[str]]] = {
    "paths": frozenset(
        {
            "evidence_dir",
            "voice_profile_dir",
            "log_dir",
            "ext_token",
            "dnsmasq_dir",
            "nftables_dir",
        }
    ),
    "ai": frozenset(
        {
            "enabled",
            "socket_name",
            "max_per_20s",
            "max_per_hour",
            "max_per_day",
            "evidence_retention_days",
        }
    ),
}
"""Keys earlier versions had (D-180, D-183). Tolerated, never used."""

_CACHED: Config | None = None


def get_config(reload: bool = False) -> Config:
    """Process-wide config singleton. `reload=True` re-reads the file."""
    global _CACHED
    if _CACHED is None or reload:
        _CACHED = Config.load()
    return _CACHED


def set_config(config: Config) -> None:
    """Install a config explicitly. Tests and `--config` flags use this."""
    global _CACHED
    _CACHED = config
