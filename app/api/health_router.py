"""Health check 엔드포인트.

- /health/live: 프로세스가 살아있는지만 확인. k8s livenessProbe.
- /health/ready: 의존성(DB, Redis, MQ) 연결까지 확인. k8s readinessProbe.
- /health: 하위호환을 위한 기존 라이브니스 alias.

readiness는 초기화 미완료 시 503을 반환해야 트래픽 라우팅이 안전하게 차단된다.
"""
import asyncio

import aio_pika
from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.client.redis_client import get_redis
from app.core.config import get_settings
from app.core.database import session_scope
from app.core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["health"])

_DEPENDENCY_TIMEOUT_SEC = 2.0


@router.get("/health")
@router.get("/health/live")
async def liveness() -> dict[str, str]:
    """프로세스 살아있음. 외부 의존성 점검 안 함."""
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness() -> JSONResponse:
    results = await asyncio.gather(
        _check_db(),
        _check_redis(),
        _check_rabbitmq(),
        return_exceptions=False,
    )
    db_ok, redis_ok, mq_ok = results
    all_ok = all([db_ok, redis_ok, mq_ok])

    body = {
        "status": "ok" if all_ok else "degraded",
        "checks": {
            "database": "ok" if db_ok else "fail",
            "redis": "ok" if redis_ok else "fail",
            "rabbitmq": "ok" if mq_ok else "fail",
        },
    }
    code = status.HTTP_200_OK if all_ok else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(status_code=code, content=body)


async def _check_db() -> bool:
    try:
        async with session_scope() as session:
            await asyncio.wait_for(
                session.execute(text("SELECT 1")), timeout=_DEPENDENCY_TIMEOUT_SEC
            )
        return True
    except Exception as exc:
        logger.warning("db readiness check failed: %s", exc)
        return False


async def _check_redis() -> bool:
    try:
        redis = get_redis()
        await asyncio.wait_for(redis.ping(), timeout=_DEPENDENCY_TIMEOUT_SEC)
        return True
    except Exception as exc:
        logger.warning("redis readiness check failed: %s", exc)
        return False


async def _check_rabbitmq() -> bool:
    settings = get_settings()
    try:
        connection = await asyncio.wait_for(
            aio_pika.connect(settings.rabbitmq_url),
            timeout=_DEPENDENCY_TIMEOUT_SEC,
        )
        await connection.close()
        return True
    except Exception as exc:
        logger.warning("rabbitmq readiness check failed: %s", exc)
        return False
