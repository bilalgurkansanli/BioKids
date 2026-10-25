"""Exception types shared across BioKids components."""

from __future__ import annotations


class BioKidsError(Exception):
    """Base class for every error BioKids raises deliberately."""


class ConfigError(BioKidsError):
    """`/etc/biokids/config.toml` is missing, malformed, or has a bad value."""


class IpcError(BioKidsError):
    """A request to the daemon socket failed."""
