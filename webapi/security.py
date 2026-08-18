from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

JWT_SECRET = os.getenv("API_JWT_SECRET", "change-me-in-production-please-use-32-plus-chars")
JWT_ALGORITHM = os.getenv("API_JWT_ALGORITHM", "HS256")
JWT_EXPIRE_MINUTES = int(os.getenv("API_JWT_EXPIRE_MINUTES", "480"))
API_USER = os.getenv("API_USER", "admin")
API_PASSWORD = os.getenv("API_PASSWORD", "admin")
API_ROLE = os.getenv("API_ROLE", "administrator")
API_OPERATOR_USER = os.getenv("API_OPERATOR_USER", "operator")
API_OPERATOR_PASSWORD = os.getenv("API_OPERATOR_PASSWORD", "operator")
API_VIEWER_USER = os.getenv("API_VIEWER_USER", "viewer")
API_VIEWER_PASSWORD = os.getenv("API_VIEWER_PASSWORD", "viewer")

bearer_scheme = HTTPBearer(auto_error=False)


def create_access_token(subject: str, role: str) -> str:
    expires = datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRE_MINUTES)
    payload: dict[str, Any] = {
        "sub": subject,
        "role": role,
        "exp": expires,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def authenticate(username: str, password: str) -> dict[str, str] | None:
    if username == API_USER and password == API_PASSWORD:
        return {"username": username, "role": API_ROLE}
    if username == API_OPERATOR_USER and password == API_OPERATOR_PASSWORD:
        return {"username": username, "role": "operator"}
    if username == API_VIEWER_USER and password == API_VIEWER_PASSWORD:
        return {"username": username, "role": "read-only"}
    return None


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> dict[str, str]:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authorization token",
        )

    token = credentials.credentials
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        ) from exc

    username = payload.get("sub")
    role = payload.get("role", "read-only")
    if not username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token subject",
        )
    return {"username": str(username), "role": str(role)}


def require_roles(*allowed_roles: str):
    def _dependency(user: dict[str, str] = Depends(get_current_user)) -> dict[str, str]:
        role = user.get("role", "read-only")
        if role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient role permissions",
            )
        return user

    return _dependency
