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

import pytest
from sqlalchemy.orm import Session

from biokids_core.clock import FakeClock
from biokids_core.config import Config, set_config
from biokids_core.db import Child, Database, Family, Parent, Policy
from biokids_core.ledger import TimeLedger
from biokids_core.types import AgeBand


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
