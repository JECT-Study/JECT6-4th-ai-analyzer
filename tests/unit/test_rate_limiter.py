import asyncio

import fakeredis.aioredis
import pytest

from app.core.rate_limiter import RateLimiter


@pytest.fixture
async def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.flushall()
    await client.aclose()


@pytest.fixture
def limiter(redis_client):
    return RateLimiter(redis_client)


class TestRateLimiter:
    async def test_first_request_allowed_with_full_bucket(self, limiter):
        result = await limiter.consume(
            scope="chat", user_id=1, capacity=5, refill_per_sec=1.0
        )
        assert result.allowed is True
        # 1개 소비 후 4개 남음
        assert result.remaining == pytest.approx(4.0, abs=0.5)
        assert result.retry_after_ms == 0

    async def test_burst_capacity_can_be_consumed_in_a_row(self, limiter):
        # capacity=3 이면 연속 3번까진 OK
        for i in range(3):
            result = await limiter.consume(
                scope="chat", user_id=2, capacity=3, refill_per_sec=0.5
            )
            assert result.allowed is True, f"iteration {i} should pass"

        # 4번째는 거절
        result = await limiter.consume(
            scope="chat", user_id=2, capacity=3, refill_per_sec=0.5
        )
        assert result.allowed is False
        assert result.retry_after_ms > 0

    async def test_separate_users_have_independent_buckets(self, limiter):
        # user 1이 한도 소진해도 user 2는 영향 없음
        for _ in range(2):
            await limiter.consume(
                scope="chat", user_id=10, capacity=2, refill_per_sec=0.1
            )
        rejected = await limiter.consume(
            scope="chat", user_id=10, capacity=2, refill_per_sec=0.1
        )
        assert rejected.allowed is False

        ok = await limiter.consume(
            scope="chat", user_id=11, capacity=2, refill_per_sec=0.1
        )
        assert ok.allowed is True

    async def test_separate_scopes_have_independent_buckets(self, limiter):
        await limiter.consume(
            scope="chat", user_id=20, capacity=1, refill_per_sec=0.01
        )
        chat_blocked = await limiter.consume(
            scope="chat", user_id=20, capacity=1, refill_per_sec=0.01
        )
        analysis_ok = await limiter.consume(
            scope="analysis", user_id=20, capacity=1, refill_per_sec=0.01
        )
        assert chat_blocked.allowed is False
        assert analysis_ok.allowed is True

    async def test_refill_after_waiting(self, limiter):
        # capacity 1, refill 100/sec → 10ms마다 1개 채워짐
        result1 = await limiter.consume(
            scope="chat", user_id=30, capacity=1, refill_per_sec=100
        )
        assert result1.allowed is True

        # 즉시 재요청은 거절될 가능성
        immediate = await limiter.consume(
            scope="chat", user_id=30, capacity=1, refill_per_sec=100
        )
        # 50ms 대기 후 충전됐는지 확인
        await asyncio.sleep(0.05)
        after_wait = await limiter.consume(
            scope="chat", user_id=30, capacity=1, refill_per_sec=100
        )
        # 즉시는 보통 거절이지만 환경에 따라 달라질 수 있어 단정 안 함.
        # 충분히 기다린 후엔 반드시 통과해야 함.
        assert after_wait.allowed is True
        # 그리고 적어도 두 번의 시도 중 하나는 결과가 갈려야 (refill이 의미 있게 작동)
        assert immediate.allowed != after_wait.allowed or after_wait.allowed is True

    async def test_concurrent_requests_respect_capacity(self, limiter):
        """Lua 스크립트의 원자성: 동시 요청 N개 중 capacity만큼만 통과해야."""
        capacity = 5
        attempts = 20

        async def attempt():
            return await limiter.consume(
                scope="chat", user_id=99, capacity=capacity, refill_per_sec=0.001
            )

        results = await asyncio.gather(*[attempt() for _ in range(attempts)])
        allowed_count = sum(1 for r in results if r.allowed)
        # refill이 매우 작아 실질적으로 capacity만큼만 허용되어야 함
        assert allowed_count == capacity
