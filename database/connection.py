"""
Database connection management.

Design decision: A `DatabaseConnection` class encapsulates engine creation and
session factory so the rest of the application never imports SQLAlchemy
directly for connection concerns.  The `get_session` context manager provides
automatic commit/rollback semantics.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from config.settings import settings
from utils.logger import get_logger

logger = get_logger(__name__)


def _configure_sqlite_pragmas(engine: Engine) -> None:
    """Enable WAL mode and foreign keys for SQLite connections."""

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection: object, _connection_record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[union-attr]
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


class DatabaseConnection:
    """
    Manages the SQLAlchemy engine and session factory lifecycle.

    Usage::

        db = DatabaseConnection()
        db.create_tables()
        with db.session() as session:
            session.add(some_model)
    """

    def __init__(self, database_url: str | None = None) -> None:
        self._url = database_url or settings.database.url
        self._engine = self._build_engine()
        self._Session = sessionmaker(bind=self._engine, expire_on_commit=False)
        logger.info("DatabaseConnection initialised - url=%s", self._url.split("@")[-1])

    def _build_engine(self) -> Engine:
        """Create the SQLAlchemy engine with appropriate settings."""
        backend = settings.database.type.strip().lower()
        url_lower = self._url.strip().lower()
        is_sqlite = url_lower.startswith("sqlite")
        is_mysql = ("mysql" in backend) or url_lower.startswith("mysql")
        engine_kwargs: dict[str, object] = {
            "echo": settings.database.echo,
            "future": True,
        }

        # MySQL transient disconnect hardening.
        if is_mysql and not is_sqlite:
            engine_kwargs.update(
                {
                    "pool_pre_ping": True,
                    "pool_recycle": int(os.getenv("DB_POOL_RECYCLE_SECONDS", "1800")),
                    "pool_size": int(os.getenv("DB_POOL_SIZE", "10")),
                    "max_overflow": int(os.getenv("DB_MAX_OVERFLOW", "20")),
                    "pool_timeout": int(os.getenv("DB_POOL_TIMEOUT_SECONDS", "30")),
                }
            )

        engine = create_engine(self._url, **engine_kwargs)
        if "sqlite" in self._url:
            _configure_sqlite_pragmas(engine)
        return engine

    @property
    def engine(self) -> Engine:
        """Expose the underlying SQLAlchemy Engine."""
        return self._engine

    def create_tables(self) -> None:
        """Create all tables defined in the metadata (idempotent)."""
        from database.models import Base  # Evitar circular import at module level

        Base.metadata.create_all(self._engine)
        logger.info("Database tables created (or already exist).")

    @contextmanager
    def session(self) -> Generator[Session, None, None]:
        """
        Provide a transactional session scope.

        Commits on success, rolls back on any exception, and always closes
        the session.
        """
        session: Session = self._Session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        """Release all pooled connections."""
        self._engine.dispose()
        logger.info("Database engine disposed.")


def bootstrap_database(database_url: str | None = None) -> None:
    """
    Backwards-compatible bootstrap helper.

    The real bootstrap logic lives in database.bootstrap to keep schema
    creation and connection management decoupled.
    """
    from database.bootstrap import bootstrap_database as _bootstrap_database

    _bootstrap_database(database_url)


# ---------------------------------------------------------------------------
# Nivel de modulo singleton helpers
# ---------------------------------------------------------------------------

_db: DatabaseConnection | None = None


def get_db() -> DatabaseConnection:
    """Return the application-wide DatabaseConnection singleton."""
    global _db
    if _db is None:
        _db = DatabaseConnection()
    return _db


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """
    Convenience context manager that provides a session from the singleton DB.

    Usage::

        with get_session() as session:
            session.add(record)
    """
    with get_db().session() as session:
        yield session
