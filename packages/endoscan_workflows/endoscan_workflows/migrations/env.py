from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from endoscan_workflows.models import Base

config = context.config
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section) or {},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    # The PRAGMA statements autobegin a SQLAlchemy transaction.  Keep an
    # explicit outer transaction so Alembic's revision marker is committed
    # together with the migration on SQLite; closing a bare connection would
    # otherwise roll the marker back while SQLite retains its DDL.
    with connectable.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
