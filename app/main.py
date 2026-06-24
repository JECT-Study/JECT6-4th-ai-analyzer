from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import (
    analysis_router,
    conversation_router,
    diagnosis_router,
    document_router,
    health_router,
    profile_router,
)
from app.api.error_handlers import register_exception_handlers
from app.client.redis_client import close_redis
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.core.tracing import setup_tracing


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    setup_tracing(app)
    yield
    await close_redis()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
        lifespan=lifespan,
    )

    register_exception_handlers(app)

    app.include_router(health_router.router)
    app.include_router(document_router.router)
    app.include_router(analysis_router.router)
    app.include_router(conversation_router.router)
    app.include_router(diagnosis_router.router)
    app.include_router(profile_router.router)

    return app


app = create_app()
