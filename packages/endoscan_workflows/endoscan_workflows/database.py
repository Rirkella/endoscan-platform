"""SQLite engine configuration and programmatic Alembic migration entry point."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker


class WorkflowDatabase:
    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
            f"sqlite:///{self.path.as_posix()}",
            connect_args={"check_same_thread": False, "timeout": 30},
            future=True,
        )
        event.listen(self.engine, "connect", self._configure_connection)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False, future=True)

    @staticmethod
    def _configure_connection(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    def migrate(self) -> None:
        config = Config()
        migrations = Path(__file__).with_name("migrations")
        config.set_main_option("script_location", migrations.as_posix())
        config.set_main_option("sqlalchemy.url", f"sqlite:///{self.path.as_posix()}")
        command.upgrade(config, "head")

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._sessions()
        try:
            with session.begin():
                yield session
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def capability(self) -> dict[str, str | bool]:
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
                journal = connection.execute(text("PRAGMA journal_mode")).scalar_one()
                foreign_keys = connection.execute(text("PRAGMA foreign_keys")).scalar_one()
            return {
                "available": True,
                "journal_mode": str(journal).lower(),
                "foreign_keys": bool(foreign_keys),
            }
        except Exception:
            return {"available": False, "journal_mode": "unknown", "foreign_keys": False}

    def dispose(self) -> None:
        self.engine.dispose()
