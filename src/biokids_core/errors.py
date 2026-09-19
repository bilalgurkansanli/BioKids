"""Exception types shared across BioKids components."""

from __future__ import annotations


class BioKidsError(Exception):
    """Base class for every error BioKids raises deliberately."""


class ConfigError(BioKidsError):
    """`/etc/biokids/config.toml` is missing, malformed, or has a bad value."""


class PolicyError(BioKidsError):
    """A policy could not be constructed from stored values."""


class LedgerError(BioKidsError):
    """The time ledger was asked to do something inconsistent."""


class IpcError(BioKidsError):
    """A request to the daemon socket failed."""


class AuthError(BioKidsError):
    """Authentication failed, or is locked out."""
