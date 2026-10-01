"""
Utilitários de inicialização do banco de dados.

Este módulo é responsável por duas tarefas:
1. Criar o banco/esquema de destino ao usar MySQL.
2. Criar todas as tabelas da aplicação depois que o esquema estiver disponível.

A inicialização é intencionalmente idempotente, para que possa ser executada
a cada inicialização.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL
from sqlalchemy.engine.url import make_url

from config.settings import settings
from database.connection import DatabaseConnection
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class BootstrapResult:
    """Resumo do resultado retornado após a conclusão da inicialização."""

    database_url: str
    database_created: bool
    tables_created: bool


def bootstrap_database(database_url: str | None = None) -> BootstrapResult:
    """
    Garante que o banco de dados configurado e suas tabelas existam.

    Para SQLite, cria somente as tabelas.
    Para MySQL, primeiro cria o esquema/banco de dados, caso ainda não exista,
    e depois cria as tabelas da aplicação.
    """
    raw_url = database_url or settings.database.url
    url = make_url(raw_url)
    database_created = False

    if url.get_backend_name() == "mysql":
        database_created = _create_mysql_database(url)

    from database import history_models  # noqa: F401  # Garante o registro dos metadados do ORM
    from database import next_phase_models  # noqa: F401  # Garante o registro dos metadados do ORM
    from database import session_models  # noqa: F401  # Garante o registro dos metadados do ORM

    connection = DatabaseConnection(raw_url)
    connection.create_tables()
    _migrate_live_accounting_schema(connection)
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
    Cria o esquema/banco de dados MySQL caso ainda não exista.

    O MySQL exige que a conexão inicial seja feita a um esquema existente no
    servidor; por isso, removemos da URL a parte referente ao banco e
    executamos uma instrução CREATE DATABASE.
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


def _migrate_live_accounting_schema(connection: DatabaseConnection) -> None:
    """Adiciona campos contábeis anuláveis sem reescrever o histórico de operações existente."""
    required_columns = {
        "trades": {
            "execution_id": "VARCHAR(36) NULL",
            "strategy": "VARCHAR(100) NULL",
            "timeframe": "VARCHAR(10) NULL",
            "risk_reward": "FLOAT NULL",
            "duration_minutes": "FLOAT NULL",
            "score": "FLOAT NULL",
            "total_fees_usdt": "FLOAT NULL",
            "gross_pnl": "FLOAT NULL",
            "original_quantity": "FLOAT NULL",
            "entry_fee_usdt": "FLOAT NULL",
            "entry_fee_allocated_usdt": "FLOAT NULL",
            "exit_fee_usdt": "FLOAT NULL",
            "fee_accounting_complete": "BOOLEAN NOT NULL DEFAULT 0",
        },
        "orders": {
            "fee_currency": "VARCHAR(20) NULL",
            "fee_usdt": "FLOAT NULL",
            "fee_source": "VARCHAR(32) NOT NULL DEFAULT 'MISSING'",
            "fee_conversion_price": "FLOAT NULL",
            "base_fee_quantity": "FLOAT NOT NULL DEFAULT 0",
        },
    }

    with connection.engine.begin() as db_connection:
        inspector = inspect(db_connection)
        table_names = set(inspector.get_table_names())
        for table_name, columns in required_columns.items():
            if table_name not in table_names:
                continue
            existing = {column["name"] for column in inspect(db_connection).get_columns(table_name)}
            for column_name, column_ddl in columns.items():
                if column_name in existing:
                    continue
                logger.info(
                    "Migrating %s table - adding missing column %s",
                    table_name,
                    column_name,
                )
                db_connection.execute(
                    text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_ddl}")
                )
