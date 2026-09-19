"""Shared library for every BioKids component.

Nothing in this package touches the system clock, the network, or systemd. It is
the part of BioKids that can be reasoned about and tested on any machine.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
