"""OpenTelemetry 트레이싱 설정.

OTLP 엔드포인트가 설정돼 있으면 활성화. 없으면 no-op.
LLM 호출 latency, DB 쿼리, Redis 호출 등을 자동 계측.

opentelemetry 패키지가 설치되지 않은 환경(테스트 등)에서는 setup_tracing이 no-op.
"""
from __future__ import annotations

import os

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def setup_tracing(app=None) -> None:
    """앱 시작 시 1회 호출.

    - opentelemetry 미설치 → no-op
    - OTEL_EXPORTER_OTLP_ENDPOINT 미설정 → no-op
    """
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        logger.info("OTLP endpoint not configured, tracing disabled")
        return

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.instrumentation.aio_pika import AioPikaInstrumentor
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.instrumentation.redis import RedisInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:
        logger.warning("opentelemetry not installed, tracing disabled: %s", exc)
        return

    from app.core.database import engine

    settings = get_settings()
    resource = Resource.create(
        {
            "service.name": settings.app_name,
            "service.version": "1.0.0",
            "deployment.environment": settings.environment,
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)

    if app is not None:
        FastAPIInstrumentor.instrument_app(app)
    SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)
    RedisInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()
    AioPikaInstrumentor().instrument()

    logger.info("tracing enabled endpoint=%s", endpoint)
