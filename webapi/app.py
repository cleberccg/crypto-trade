from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config.settings import settings
from database.bootstrap import bootstrap_database
from webapi.mock_api_router import router as mock_platform_router
from webapi.routers import router
from webapi.schemas import HealthResponse

_CORS_ORIGINS_ENV = os.getenv("API_CORS_ORIGINS", "")
_ALLOW_ALL_ORIGINS = os.getenv("APP_ENV", "development").lower() == "development"


def create_app() -> FastAPI:
    app = FastAPI(
        title="Crypto Trading Dashboard API",
        version="1.0.0",
        description="REST and WebSocket API for the quantitative trading dashboard.",
    )

    if _ALLOW_ALL_ORIGINS:
        # Dev mode: accept any origin so CORS never blocks local frontends
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    else:
        origins = [
            o.strip()
            for o in (_CORS_ORIGINS_ENV or "http://localhost:5173,http://127.0.0.1:5173").split(",")
            if o.strip()
        ]
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.on_event("startup")
    def _startup() -> None:
        bootstrap_database(settings.database.url)

    @app.get("/health", response_model=HealthResponse, tags=["system"])
    def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            timestamp=datetime.now(timezone.utc),
            database_url=settings.database.url,
        )

    app.include_router(router, prefix="/api/v1")
    app.include_router(mock_platform_router, prefix="/api/v1")
    return app


app = create_app()
