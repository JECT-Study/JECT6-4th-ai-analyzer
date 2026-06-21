"""AnalysisWorker._resolve_document_id() 단위 테스트.

await redis.get() 수정(PR #2)이 실제로 코루틴이 아닌 값을 반환하는지 검증한다.
"""
from __future__ import annotations

import fakeredis.aioredis
import pytest

from app.worker.analysis_worker import AnalysisWorker


@pytest.fixture
async def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.flushall()
    await client.aclose()


@pytest.fixture
def worker():
    w = object.__new__(AnalysisWorker)
    return w


async def test_resolve_document_id_returns_int_when_key_exists(worker, redis_client):
    """ingest:done:{correlation_id} 키가 있으면 정수 document_id를 반환한다.

    이전에 await 없이 redis.get()을 호출하면 코루틴 객체가 반환돼
    int() 변환 시 TypeError가 발생했다. 수정 후 정상 동작을 확인한다.
    """
    correlation_id = "corr-test-1"
    await redis_client.set(f"ingest:done:{correlation_id}", "777", ex=3600)

    from unittest.mock import patch
    with patch("app.worker.analysis_worker.get_redis", return_value=redis_client):
        result = await worker._resolve_document_id(correlation_id)

    assert result == 777
    assert isinstance(result, int)


async def test_resolve_document_id_returns_none_when_key_missing(worker, redis_client):
    """키가 없으면 None을 반환한다."""
    from unittest.mock import patch
    with patch("app.worker.analysis_worker.get_redis", return_value=redis_client):
        result = await worker._resolve_document_id("nonexistent-corr-id")

    assert result is None


async def test_resolve_document_id_returns_none_on_redis_error(worker):
    """Redis 오류 시 예외를 삼키고 None을 반환한다."""
    from unittest.mock import AsyncMock, patch
    broken_redis = AsyncMock()
    broken_redis.get.side_effect = ConnectionError("redis down")

    with patch("app.worker.analysis_worker.get_redis", return_value=broken_redis):
        result = await worker._resolve_document_id("any-corr-id")

    assert result is None
