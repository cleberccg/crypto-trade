"""
Database bootstrap utilities.

This module is responsible for two things:
1. Creating the target database/schema when using MySQL.
2. Creating all application tables after the schema is available.

The bootstrap is intentionally idempotent so it can run on every startup.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.engine.url import make_url

from config.settings import settings
from database.connection import DatabaseConnection
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class BootstrapResult:
    """Result summary returned after the bootstrap completes."""

    database_url: str
    database_created: bool
    tables_created: bool


def bootstrap_database(database_url: str | None = None) -> BootstrapResult:
    """
    Ensure the configured database and tables exist.

    For SQLite, this only creates tables.
    For MySQL, this first creates the schema/database if it does not exist,
    then creates the application tables.
    """
    raw_url = database_url or settings.database.url
    url = make_url(raw_url)
    database_created = False

    if url.get_backend_name() == "mysql":
        database_created = _create_mysql_database(url)

    from database import history_models  # noqa: F401  # ensure ORM metadata is registered
    from database import next_phase_models  # noqa: F401  # ensure ORM metadata is registered
    from database import session_models  # noqa: F401  # ensure ORM metadata is registered

    connection = DatabaseConnection(raw_url)
    connection.create_tables()
    if url.get_backend_name() == "mysql":
        _migrate_mysql_trade_table(connection)
    connection.dispose()

    logger.info(
        "Database bootstrap complete - url=%s database_created=%s tables_created=%s",
        raw_url,
        database_created,
        True,
    )
    return BootstrapResult(
        database_url=raw_url,
        database_created=database_created,
        tables_created=True,
    )


def _create_mysql_database(url: URL) -> bool:
    """
    Create the MySQL schema/database if it does not exist yet.

    MySQL requires connecting to an existing server schema first, so we strip
    the database portion from the URL and issue a CREATE DATABASE statement.
    """
    database_name = url.database
    if not database_name:
        raise ValueError("MySQL DATABASE_URL must include a database name.")

    # Conecta primeiro a um schema de sistema existente; o banco alvo pode
    # ainda nao existir, entao conectar diretamente nele falharia.
    admin_url = url.set(database="mysql")
    engine = create_engine(admin_url, future=True)
    safe_database_name = database_name.replace("`", "")

    with engine.connect() as connection:
        connection = connection.execution_options(isolation_level="AUTOCOMMIT")
        connection.execute(
            text(
                f"CREATE DATABASE IF NOT EXISTS `{safe_database_name}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        )

    engine.dispose()
    logger.info("MySQL database ensured - name=%s", safe_database_name)
    return True


def _migrate_mysql_trade_table(connection: DatabaseConnection) -> None:
    """Bring the legacy trades table in sync with the current ORM model."""
    required_columns = {
        "execution_id": "VARCHAR(36) NULL",
        "strategy": "VARCHAR(100) NULL",
        "timeframe": "VARCHAR(10) NULL",
        "risk_reward": "FLOAT NULL",
        "duration_minutes": "FLOAT NULL",
        "score": "FLOAT NULL",
    }

    with connection.engine.connect() as db_connection:
        db_connection = db_connection.execution_options(isolation_level="AUTOCOMMIT")
        existing_columns = {
            row[0]
            for row in db_connection.execute(
                text(
                    """
                    SELECT COLUMN_NAME
                    FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME = 'trades'
                    """
                )
            )
        }

        missing_columns = [
            (name, ddl)
            for name, ddl in required_columns.items()
            if name not in existing_columns
        ]

        for column_name, column_ddl in missing_columns:
            logger.info(
                "Migrating trades table - adding missing column %s (%s)",
                column_name,
                column_ddl,
            )
            db_connection.execute(
                text(f"ALTER TABLE trades ADD COLUMN {column_name} {column_ddl}")
            )

    if missing_columns:
        logger.info(
            "Trades table migration complete - added %d missing columns.",
            len(missing_columns),
        )
