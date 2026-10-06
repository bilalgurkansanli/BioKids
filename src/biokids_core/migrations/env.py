"""Alembic environment.

The URL comes from BioKids' own config rather than from `alembic.ini`, so that
`alembic upgrade head` does the right thing in three places without editing a
file: in the postinst as root against `/var/lib/biokids`, in CI against a
temporary directory, and on a developer machine under `BIOKIDS_PREFIX`.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from biokids_core.config import get_config
from biokids_core.db.models import Base

config = context.config

if config.config_file_name is not None:
    # Without this, fileConfig switches off every logger that already exists:
    # run in-process (the migration test, `biokids-admin`), the rest of the
    # process went quiet after the first upgrade.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

_biokids = get_config()
# The postinst runs this before anything has opened the database, so the
# directory may not exist yet. SQLite will not create it for us.
_biokids.paths.db_path.parent.mkdir(parents=True, exist_ok=True)
config.set_main_option("sqlalchemy.url", f"sqlite:///{_biokids.paths.db_path}")


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite cannot ALTER a column; batch mode rebuilds the table instead.
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
