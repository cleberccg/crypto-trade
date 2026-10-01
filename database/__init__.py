"""
Pacote de banco de dados: mecanismo SQLAlchemy, fábrica de sessões, inicialização e registro de modelos.
"""
from database.bootstrap import BootstrapResult, bootstrap_database
from database.connection import DatabaseConnection, get_session
from database.models import Base
from database import next_phase_models  # noqa: F401

__all__ = ["Base", "BootstrapResult", "DatabaseConnection", "bootstrap_database", "get_session"]
