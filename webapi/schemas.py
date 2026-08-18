from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str


class PaginatedMeta(BaseModel):
    page: int
    page_size: int
    total: int


class PaginatedResponse(BaseModel):
    meta: PaginatedMeta
    items: list[dict]


class HealthResponse(BaseModel):
    status: str
    timestamp: datetime
    database_url: str


class NightRunnerHealthResponse(BaseModel):
    status: str
    timestamp: datetime
    execution_id: str | None = None
    pid: int | None = None
    state: str
    heartbeat: str | None = None
    last_checkpoint: str | None = None
    last_log: str | None = None
    last_database_update: str | None = None
    last_combination: str | None = None
    eta_minutes: float | None = None
