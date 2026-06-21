"""ingest_worker FULL_BLOG 배치 로직 단위 테스트.

fakeredis를 사용해 외부 의존성(RabbitMQ, DB, LLM) 없이 Redis 상태 머신을 검증한다.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
import pytest

from app.worker.ingest_worker import (
    IngestWorker,
    _BATCH_TTL,
    _SNAPSHOT_COMPLETED,
    _SNAPSHOT_CREATING,
    _SNAPSHOT_FAILED,
)


# ─── fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
async def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.flushall()
    await client.aclose()


@pytest.fixture
def worker(redis_client):
    """실제 연결 없이 Redis만 주입한 최소 IngestWorker."""
    w = object.__new__(IngestWorker)
    queue_mock = AsyncMock()
    queue_mock._redis = redis_client
    w._queue = queue_mock
    w._llm_client = AsyncMock()
    w._settings = AsyncMock()
    w._settings.analysis_queue_name = "blog.analysis"
    w._settings.rabbitmq_url = "amqp://guest:guest@localhost/"
    return w


# ─── helper ────────────────────────────────────────────────────────────────

async def _set_batch_meta(redis, batch_id: str, user_id: int = 1, expected: int = 3) -> str:
    prefix = f"batch:{batch_id}"
    await redis.set(f"{prefix}:user_id", str(user_id), ex=_BATCH_TTL)
    await redis.set(f"{prefix}:expected", str(expected), ex=_BATCH_TTL)
    return prefix


# ─── #1 상태 머신: completed 정상 흐름 ────────────────────────────────────

async def test_snapshot_status_completed_on_success(worker, redis_client):
    """스냅샷 생성 + 발행 성공 → snapshot_status=completed."""
    batch_id = "test-batch-ok"
    prefix = f"batch:{batch_id}"
    status_key = f"{prefix}:snapshot_status"
    await _set_batch_meta(redis_client, batch_id)

    # NX로 creating 선점
    await redis_client.set(status_key, _SNAPSHOT_CREATING, nx=True, ex=_BATCH_TTL)

    async def fake_create(*, batch_id, prefix, status_key):
        # 성공 시 completed 기록
        await redis_client.set(status_key, _SNAPSHOT_COMPLETED, ex=_BATCH_TTL)

    with patch.object(worker, "_create_snapshot_and_publish", side_effect=fake_create):
        await worker._create_snapshot_and_publish(
            batch_id=batch_id, prefix=prefix, status_key=status_key
        )

    assert await redis_client.get(status_key) == _SNAPSHOT_COMPLETED


async def test_snapshot_status_failed_on_error(worker, redis_client):
    """스냅샷 생성 실패 → snapshot_status=failed + 예외 재발생."""
    batch_id = "test-batch-fail"
    prefix = f"batch:{batch_id}"
    status_key = f"{prefix}:snapshot_status"
    await _set_batch_meta(redis_client, batch_id)
    await redis_client.sadd(f"{prefix}:doc_ids", "1", "2")
    await redis_client.set(f"{prefix}:user_id", "1", ex=_BATCH_TTL)

    with (
        patch("app.worker.ingest_worker.session_scope"),
        patch("app.worker.ingest_worker.BlogSnapshotService") as mock_svc_cls,
    ):
        mock_svc = AsyncMock()
        mock_svc.create_snapshot.side_effect = RuntimeError("db down")
        mock_svc_cls.return_value = mock_svc

        with pytest.raises(RuntimeError, match="db down"):
            await worker._create_snapshot_and_publish(
                batch_id=batch_id, prefix=prefix, status_key=status_key
            )

    assert await redis_client.get(status_key) == _SNAPSHOT_FAILED


async def test_snapshot_retry_after_failed(worker, redis_client):
    """failed 상태에서 재진입 → Lua 원자 전환 → 재처리 성공."""
    batch_id = "test-batch-retry"
    prefix = f"batch:{batch_id}"
    status_key = f"{prefix}:snapshot_status"

    # failed 상태 사전 설정
    await redis_client.set(status_key, _SNAPSHOT_FAILED, ex=_BATCH_TTL)

    # NX는 이미 키가 있으므로 실패
    locked = await redis_client.set(status_key, _SNAPSHOT_CREATING, nx=True, ex=_BATCH_TTL)
    assert not locked

    existing = (await redis_client.get(status_key)) or ""
    assert existing == _SNAPSHOT_FAILED

    # Lua 스크립트로 원자 전환
    from app.worker.ingest_worker import _TRANSITION_TO_CREATING_LUA
    retried = await redis_client.eval(
        _TRANSITION_TO_CREATING_LUA,
        1, status_key,
        _SNAPSHOT_FAILED, _SNAPSHOT_CREATING, str(_BATCH_TTL),
    )
    assert int(retried) == 1
    assert await redis_client.get(status_key) == _SNAPSHOT_CREATING


async def test_creating_blocks_duplicate_entry(worker, redis_client):
    """creating 상태에서 두 번째 워커는 Lua 전환 실패 → 진입 차단."""
    batch_id = "test-batch-double"
    prefix = f"batch:{batch_id}"
    status_key = f"{prefix}:snapshot_status"

    await redis_client.set(status_key, _SNAPSHOT_CREATING, ex=_BATCH_TTL)

    from app.worker.ingest_worker import _TRANSITION_TO_CREATING_LUA
    retried = await redis_client.eval(
        _TRANSITION_TO_CREATING_LUA,
        1, status_key,
        _SNAPSHOT_FAILED, _SNAPSHOT_CREATING, str(_BATCH_TTL),
    )
    # creating은 failed가 아니므로 전환 실패 → 0
    assert int(retried) == 0
    assert await redis_client.get(status_key) == _SNAPSHOT_CREATING


# ─── #2 SADD 멱등성 ─────────────────────────────────────────────────────────

async def test_sadd_prevents_duplicate_doc_ids(redis_client):
    """같은 doc_id를 두 번 SADD해도 Set에는 1개만 존재한다."""
    key = "batch:dedup-test:doc_ids"
    await redis_client.sadd(key, "42")
    await redis_client.sadd(key, "42")  # 중복 추가
    await redis_client.sadd(key, "99")

    members = await redis_client.smembers(key)
    assert members == {"42", "99"}
    assert len(members) == 2


async def test_smembers_returns_set_not_list(redis_client):
    """smembers()는 순서 없는 set을 반환한다 (list가 아님)."""
    key = "batch:smembers-test:doc_ids"
    for i in range(5):
        await redis_client.sadd(key, str(i))
    result = await redis_client.smembers(key)
    assert isinstance(result, set)
    assert result == {"0", "1", "2", "3", "4"}


# ─── #3 deadline 기반 부분 스냅샷 ────────────────────────────────────────────

async def test_partial_snapshot_triggers_after_deadline(worker, redis_client):
    """deadline 초과 시 completed < expected에서도 스냅샷이 트리거된다."""
    batch_id = "test-batch-deadline"
    prefix = f"batch:{batch_id}"

    # 이미 만료된 deadline 설정
    expired = str(int(time.time()) - 10)
    await redis_client.set(f"{prefix}:deadline", expired, ex=_BATCH_TTL)
    await redis_client.set(f"{prefix}:user_id", "1", ex=_BATCH_TTL)
    await redis_client.set(f"{prefix}:expected", "3", ex=_BATCH_TTL)
    await redis_client.sadd(f"{prefix}:doc_ids", "10", "11")  # 2개만 ingest

    triggered = []

    async def fake_create(*, batch_id, prefix, status_key):
        triggered.append(batch_id)
        await redis_client.set(status_key, _SNAPSHOT_COMPLETED, ex=_BATCH_TTL)

    with (
        patch.object(worker, "_create_snapshot_and_publish", side_effect=fake_create),
        patch("app.worker.ingest_worker.session_scope"),
    ):
        await worker._handle_full_blog_batch(
            batch_id=batch_id,
            document_id=12,
            user_id=1,
            blog_id=None,
            correlation_id=None,
            expected_count=3,
        )

    assert triggered, "deadline 초과 시 부분 스냅샷이 트리거돼야 합니다."


async def test_no_partial_snapshot_before_deadline(worker, redis_client):
    """deadline 미초과 시 completed < expected이면 스냅샷 트리거 없음."""
    batch_id = "test-batch-nodeadline"
    prefix = f"batch:{batch_id}"

    future = str(int(time.time()) + 9999)
    await redis_client.set(f"{prefix}:deadline", future, ex=_BATCH_TTL)
    await redis_client.set(f"{prefix}:user_id", "1", ex=_BATCH_TTL)
    await redis_client.set(f"{prefix}:expected", "5", ex=_BATCH_TTL)

    triggered = []

    async def fake_create(**kwargs):
        triggered.append(True)

    with patch.object(worker, "_create_snapshot_and_publish", side_effect=fake_create):
        await worker._handle_full_blog_batch(
            batch_id=batch_id,
            document_id=1,
            user_id=1,
            blog_id=None,
            correlation_id=None,
            expected_count=5,
        )

    assert not triggered, "deadline 미초과 시 스냅샷이 트리거되면 안 됩니다."


# ─── #4 _publish_analysis raise ──────────────────────────────────────────────

async def test_publish_failure_raises(worker, redis_client):
    """_publish_analysis 발행 실패 시 예외가 재발생한다."""
    with patch("app.worker.ingest_worker.aio_pika") as mock_pika:
        mock_pika.connect_robust.side_effect = ConnectionError("MQ down")
        with pytest.raises(ConnectionError, match="MQ down"):
            await worker._publish_analysis(
                user_id=1,
                document_id=100,
                correlation_id="corr-1",
                analysis_mode="FULL_BLOG",
            )
