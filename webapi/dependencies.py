from __future__ import annotations

from collections.abc import Generator

from fastapi import Depends
from sqlalchemy.orm import Session

from database.connection import get_session
from webapi.security import get_current_user, require_roles


def get_db_session() -> Generator[Session, None, None]:
    with get_session() as session:
        yield session


def require_user(_user: dict[str, str] = Depends(get_current_user)) -> dict[str, str]:
    return _user


def require_operator_or_admin(
    _user: dict[str, str] = Depends(require_roles("operator", "administrator")),
) -> dict[str, str]:
    return _user


def require_admin(_user: dict[str, str] = Depends(require_roles("administrator"))) -> dict[str, str]:
    return _user
