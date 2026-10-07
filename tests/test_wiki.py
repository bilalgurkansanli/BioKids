"""D-190 — the offline Wikipedia as a tile a parent may enable."""

from __future__ import annotations

import socket
from pathlib import Path

from biokids_core.catalog import load_catalog
from biokids_core.config import Config
from biokids_daemon.dnsfilter import FilterFiles, render_conf
from biokids_shell.app import local_service_up


def test_the_tile_is_for_eight_and_up_and_never_preselected(config: Config) -> None:
    [wiki] = [entry for entry in load_catalog(config=config) if entry.slug == "vikipedi"]
    assert wiki.is_web and wiki.min_age == 8
    assert wiki.suggest is False, "a parent ticks it on purpose"
    assert wiki.allowed_patterns() == ("127.0.0.1:7877",)


def test_a_tile_on_this_machine_never_reaches_dnsmasq(tmp_path: Path) -> None:
    """`server=/127.0.0.1:7877/#` is a line dnsmasq refuses, and a dnsmasq that
    will not start is every child without internet."""
    conf = render_conf(FilterFiles(tmp_path), allowed_hosts=["127.0.0.1:7877", "eba.gov.tr"])
    assert "server=/eba.gov.tr/#" in conf
    assert "127.0.0.1" not in "".join(line for line in conf.splitlines() if "server=" in line)


def test_the_tile_waits_for_the_service() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    assert local_service_up(f"http://127.0.0.1:{closed_port}/") is False

    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        assert local_service_up(f"http://127.0.0.1:{port}/") is True


def test_a_tile_elsewhere_is_always_drawn() -> None:
    assert local_service_up("https://www.eba.gov.tr") is True
