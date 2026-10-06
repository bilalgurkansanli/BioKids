"""The migrations build exactly the schema the models describe.

The rest of the suite builds its databases with `create_all()`, so a model
change without a migration passed every test and failed on the first upgrade of
a real machine. This runs the real chain, `alembic upgrade head` on an empty
file, and compares the result with the models.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config as AlembicConfig
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from biokids_core.config import Config
from biokids_core.db.models import Base

ROOT = Path(__file__).resolve().parent.parent


def test_upgrading_an_empty_database_gives_the_models_schema(config: Config) -> None:
    config.paths.db_path.parent.mkdir(parents=True, exist_ok=True)
    alembic = AlembicConfig(str(ROOT / "alembic.ini"))
    alembic.set_main_option(
        "script_location", str(ROOT / "src" / "biokids_core" / "migrations")
    )
    command.upgrade(alembic, "head")

    engine = create_engine(f"sqlite:///{config.paths.db_path}")
    try:
        with engine.connect() as connection:
            differences = compare_metadata(
                MigrationContext.configure(connection), Base.metadata
            )
    finally:
        engine.dispose()
    assert differences == [], f"the models and the migrations disagree: {differences}"


def test_every_migration_can_be_undone(config: Config) -> None:
    config.paths.db_path.parent.mkdir(parents=True, exist_ok=True)
    alembic = AlembicConfig(str(ROOT / "alembic.ini"))
    alembic.set_main_option(
        "script_location", str(ROOT / "src" / "biokids_core" / "migrations")
    )
    command.upgrade(alembic, "head")
    command.downgrade(alembic, "base")
    command.upgrade(alembic, "head")
