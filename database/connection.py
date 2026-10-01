"""
Gerenciamento da conexão com o banco de dados.

Decisão de projeto: a classe `DatabaseConnection` encapsula a criação do
mecanismo e da fábrica de sessões, para que o restante da aplicação não
importe SQLAlchemy diretamente para lidar com conexões. O gerenciador de
contexto `get_session` fornece semântica automática de commit/rollback.
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
    """Habilita o modo WAL e as chaves estrangeiras nas conexões SQLite."""

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection: object, _connection_record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[union-attr]
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


class DatabaseConnection:
    """
    Gerencia o ciclo de vida do mecanismo SQLAlchemy e da fábrica de sessões.

    Uso::

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
        """Cria o mecanismo SQLAlchemy com as configurações apropriadas."""
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
        """Disponibiliza o Engine SQLAlchemy subjacente."""
        return self._engine

    def create_tables(self) -> None:
        """Cria todas as tabelas definidas nos metadados (operação idempotente)."""
        from database.models import Base  # Evitar circular import at module level

        Base.metadata.create_all(self._engine)
        logger.info("Database tables created (or already exist).")

    @contextmanager
    def session(self) -> Generator[Session, None, None]:
        """
        Fornece um escopo transacional para a sessão.

        Efetua commit em caso de sucesso, rollback diante de qualquer exceção
        e sempre fecha a sessão.
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
        """Libera todas as conexões do pool."""
        self._engine.dispose()
        logger.info("Database engine disposed.")


def bootstrap_database(database_url: str | None = None) -> None:
    """
    Função auxiliar de inicialização compatível com versões anteriores.

    A lógica de inicialização propriamente dita fica em database.bootstrap,
    mantendo desacopladas a criação do esquema e o gerenciamento de conexões.
    """
    from database.bootstrap import bootstrap_database as _bootstrap_database

    _bootstrap_database(database_url)


# ---------------------------------------------------------------------------
# Nivel de modulo singleton helpers
# ---------------------------------------------------------------------------

_db: DatabaseConnection | None = None


def get_db() -> DatabaseConnection:
    """Retorna a instância singleton de DatabaseConnection usada pela aplicação."""
    global _db
    if _db is None:
        _db = DatabaseConnection()
    return _db


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """
    Gerenciador de contexto conveniente que fornece uma sessão do banco singleton.

    Uso::

        with get_session() as session:
            session.add(record)
    """
    with get_db().session() as session:
        yield session
