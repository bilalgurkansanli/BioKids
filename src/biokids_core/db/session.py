"""Engine and session construction.

Three things are set on every connection and none of them are optional:

* **WAL** — a reader (the panel) must never block the writer (the daemon).
* **`foreign_keys=ON`** — SQLite ignores foreign keys unless asked, per connection.
* **`busy_timeout`** — two processes share this file; a locked database should
  wait, not raise.

The file is created 0600 and owned by whoever creates it, which in production is
root. The panel and the daemon both run as root (`SECURITY.md` §2); nothing else
opens this file.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from biokids_core.config import Config, get_config
from biokids_core.db.models import Base

BUSY_TIMEOUT_MS = 5_000


def _configure_connection(dbapi_connection: Any, _record: Any) -> None:
    if not isinstance(dbapi_connection, sqlite3.Connection):  # pragma: no cover
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    finally:
        cursor.close()


def create_db_engine(config: Config | None = None, *, echo: bool = False) -> Engine:
    config = config or get_config()
    db_path = config.paths.db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(
        f"sqlite:///{db_path}",
        echo=echo,
        future=True,
        connect_args={"timeout": BUSY_TIMEOUT_MS / 1000},
    )
    event.listen(engine, "connect", _configure_connection)

    # Touch the file through the engine so the pragmas above run at least once,
    # then tighten the mode. A family's usage history is not world-readable.
    with engine.connect():
        pass
    _restrict_mode(db_path)
    return engine


def _restrict_mode(path: Path) -> None:
    """chmod 0600, and the same for the WAL sidecars SQLite creates beside it."""
    if os.name != "posix":  # pragma: no cover - development hosts only
        return
    sidecars = (path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm"))
    for candidate in (path, *sidecars):
        with contextlib.suppress(OSError):
            candidate.chmod(stat.S_IRUSR | stat.S_IWUSR)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def create_all(engine: Engine) -> None:
    """Create the schema directly.

    Production always goes through Alembic (`alembic upgrade head` in `postinst`).
    This exists for tests and for `dev/reset.sh`, where a migration chain adds
    nothing but seconds.
    """
    Base.metadata.create_all(engine)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Commit on success, roll back on failure, always close."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


class Database:
    """A small handle bundling the engine and its session factory.

    Every component takes one of these rather than reaching for a global, which
    is what makes the test suite able to run four independent databases at once.
    """

    def __init__(self, config: Config | None = None, *, echo: bool = False) -> None:
        self.config = config or get_config()
        self.engine = create_db_engine(self.config, echo=echo)
        self.factory = create_session_factory(self.engine)

    def create_all(self) -> None:
        create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        with session_scope(self.factory) as session:
            yield session

    def dispose(self) -> None:
        self.engine.dispose()
