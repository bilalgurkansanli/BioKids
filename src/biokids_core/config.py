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

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Final, Self, get_args, get_origin, get_type_hints

from biokids_core.errors import ConfigError

DEFAULT_CONFIG_PATH: Final = Path("/etc/biokids/config.toml")
ENV_CONFIG_PATH: Final = "BIOKIDS_CONFIG"
ENV_PREFIX_DIR: Final = "BIOKIDS_PREFIX"
"""Points every absolute path at a scratch root. Used by tests and `dev/reset.sh`."""


@dataclass(frozen=True, slots=True)
class PathsConfig:
    config_dir: Path = Path("/etc/biokids")
    data_dir: Path = Path("/var/lib/biokids")
    db_path: Path = Path("/var/lib/biokids/biokids.db")
    evidence_dir: Path = Path("/var/lib/biokids/evidence")
    voice_profile_dir: Path = Path("/var/lib/biokids/voices")
    voice_cache_dir: Path = Path("/var/lib/biokids/voice-cache")
    model_dir: Path = Path("/var/lib/biokids/models")
    log_dir: Path = Path("/var/log/biokids")
    run_dir: Path = Path("/run/biokids")
    daemon_socket: Path = Path("/run/biokids/daemon.sock")
    shell_socket: Path = Path("/run/biokids/shell.sock")
    """The child launcher's socket. World-connectable by necessity — the child
    is not in the `biokids` group — and authorised by the peer's uid instead,
    which the kernel supplies and nothing in a child's session can forge."""
    share_dir: Path = Path("/usr/share/biokids")
    secret_key: Path = Path("/etc/biokids/secret.key")
    ext_token: Path = Path("/etc/biokids/ext.token")
    chromium_policy_dir: Path = Path("/etc/chromium/policies/managed")
    dnsmasq_dir: Path = Path("/etc/dnsmasq.d")
    nftables_dir: Path = Path("/etc/nftables.d")

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
    kiosk_command: str = "chromium"
    kiosk_restart_seconds: int = 2


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
    espeak_binary: str = "espeak-ng"
    """The fallback engine, tried when Piper is absent. It usually is: piper-tts
    is not in Debian 13 and the last Turkish Piper voice is CC-BY-NC-SA."""
    quiet_hours: str = ""
    """Empty means never quiet. Otherwise a window like "21:00-08:00"."""


@dataclass(frozen=True, slots=True)
class AiConfig:
    enabled: bool = False
    socket_name: str = "biokids-ai.sock"
    max_per_20s: int = 1
    max_per_hour: int = 40
    max_per_day: int = 200
    evidence_retention_days: int = 7


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
