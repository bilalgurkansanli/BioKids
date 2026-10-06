"""Shared fixtures.

Every test gets its own database in a temporary directory and its own config
pointed at that directory, so nothing here touches `/var/lib/biokids` and the
suite runs unprivileged on any machine — including the Windows host the project
is developed from.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from biokids_core import provision, remote
from biokids_core.clock import FakeClock
from biokids_core.config import Config, set_config
from biokids_core.db import Child, Database, Family, Parent, Policy
from biokids_core.ledger import TimeLedger
from biokids_core.types import AgeBand


@pytest.fixture(autouse=True)
def no_real_system_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test reaches systemctl, adduser or tailscale on the machine running it.

    Found by running the suite under strace on a Debian 13 VM (D-154). Adding a
    child in the panel tests ran the real `systemctl start --wait
    biokids-provision@elif.service`, and the remote-access step ran the real
    `tailscale status --json`. As an ordinary user the first is refused; as root
    on a machine with BioKids installed it creates real accounts. On the Windows
    host neither program exists, which is why nobody noticed.

    So every test starts on a machine that has neither - the one the tests were
    written against. A test that needs one fakes it explicitly, as
    test_provision.py and test_remote.py do.
    """
    monkeypatch.setattr(provision, "_find", lambda _program: None)
    # The DNS filter loads nftables rules and restarts dnsmasq (D-167): on a
    # developer's machine, as root, that would filter their own DNS.
    from biokids_daemon import dnsfilter

    monkeypatch.setattr(dnsfilter, "_run", lambda _argv: True)
    # The once-a-minute check asks the filter on 127.0.0.1:5354 (D-172); a test
    # machine has no filter there, and a real one would answer for the machine.
    monkeypatch.setattr(dnsfilter, "dns_query_blocked", lambda *_a, **_k: True)
    # Every supervisor start renders the blocklist: 291,000 domains, ten
    # megabytes per test, 600 MB of /tmp per run. The lists themselves are
    # tested in test_dnsfilter.py, which imports load_domains directly.
    real_load_domains = dnsfilter.load_domains

    def small_lists(
        lists_dir: Path = dnsfilter.LISTS_DIR, categories: Any = dnsfilter.CATEGORIES
    ) -> set[str]:
        if lists_dir == dnsfilter.LISTS_DIR:
            return {dnsfilter.CANARY, "dns.google"}
        return real_load_domains(lists_dir, categories)

    monkeypatch.setattr(dnsfilter, "load_domains", small_lists)

    # The lockdown's own checks (D-172) ask the real logind about a session id
    # and the real biokids-child group. On a machine with BioKids installed
    # those exist - "c1" is the greeter's session - and a test must never end
    # one. Tests about them patch these back in.
    from biokids_core import admin

    monkeypatch.setattr(admin, "LOCK_SETTLE_SECONDS", 0.0)
    monkeypatch.setattr(admin, "session_property", lambda _s, _n: "")
    monkeypatch.setattr(admin, "terminate_session", lambda _s: False)
    monkeypatch.setattr(admin, "group_members", lambda _g: set())
    monkeypatch.setattr(
        remote,
        "run_command",
        lambda _argv, _timeout: remote.CommandResult(returncode=127, missing=True),
    )


@pytest.fixture(autouse=True)
def every_app_is_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The launcher draws only apps whose program exists (D-164); the machine
    running the suite has none of them. A test about a missing app says so."""
    import biokids_shell.app

    monkeypatch.setattr(biokids_shell.app, "program_installed", lambda _program: True)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    cfg = Config.load(tmp_path / "nonexistent.toml").with_prefix(tmp_path)
    set_config(cfg)
    return cfg


@pytest.fixture
def db(config: Config) -> Iterator[Database]:
    database = Database(config)
    database.create_all()
    yield database
    database.dispose()


@pytest.fixture
def session(db: Database) -> Iterator[Session]:
    with db.session() as s:
        yield s


@pytest.fixture
def clock() -> FakeClock:
    """A Monday at 16:00, so weekday quotas and afternoon windows both apply."""
    return FakeClock(wall=dt.datetime(2026, 9, 21, 16, 0, 0))


@pytest.fixture
def family(session: Session) -> Family:
    row = Family(locale="tr", timezone="Europe/Istanbul")
    session.add(row)
    session.commit()
    return row


@pytest.fixture
def parent(session: Session, family: Family) -> Parent:
    row = Parent(display_name="Anne")
    session.add(row)
    session.commit()
    return row


@pytest.fixture
def child(session: Session, family: Family) -> Child:
    row = Child(
        unix_user="elif",
        display_name="Elif",
        birth_year=2020,
        age_band=AgeBand.YOUNG.value,
        locale="tr",
    )
    session.add(row)
    session.flush()
    session.add(Policy(child_id=row.id))
    # Committed rather than flushed: tests that open a second connection must not
    # find the first one still holding a write lock on the database.
    session.commit()
    return row


@pytest.fixture
def ledger(session: Session, clock: FakeClock, config: Config) -> TimeLedger:
    led = TimeLedger(session, clock, config)
    led.begin()
    return led
