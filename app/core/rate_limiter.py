"""사용자별 토큰 버킷 레이트 리미터.

Redis + Lua 스크립트로 원자적 처리. 멀티 인스턴스 환경에서도 정확히 동작.
"""
from dataclasses import dataclass

from redis.asyncio import Redis

# KEYS[1] = 버킷 키
# ARGV[1] = capacity (최대 토큰)
# ARGV[2] = refill_per_sec (초당 충전량)
# ARGV[3] = now (현재 timestamp ms)
# ARGV[4] = requested (소비할 토큰)
# return: {allowed(0/1), remaining_tokens, retry_after_ms}
_LUA_TOKEN_BUCKET = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_per_sec = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local requested = tonumber(ARGV[4])

local data = redis.call('HMGET', key, 'tokens', 'updated_at')
local tokens = tonumber(data[1])
local updated_at = tonumber(data[2])

if tokens == nil then
    tokens = capacity
    updated_at = now
end

local elapsed_ms = math.max(0, now - updated_at)
local refill = (elapsed_ms / 1000) * refill_per_sec
tokens = math.min(capacity, tokens + refill)

local allowed = 0
local retry_after_ms = 0
if tokens >= requested then
    tokens = tokens - requested
    allowed = 1
else
    local deficit = requested - tokens
    retry_after_ms = math.ceil((deficit / refill_per_sec) * 1000)
end

redis.call('HMSET', key, 'tokens', tokens, 'updated_at', now)
-- 충전이 capacity까지 차는 시간 + 여유
local ttl_ms = math.ceil((capacity / refill_per_sec) * 1000) + 60000
redis.call('PEXPIRE', key, ttl_ms)

return {allowed, tokens, retry_after_ms}
"""


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    remaining: float
    retry_after_ms: int


class RateLimiter:
    """사용자별/리소스별 레이트 리미터.

    capacity = 버킷 최대 크기 (burst 허용량)
    refill_per_sec = 초당 충전 토큰 수 (sustained rate)
    """

    KEY_TEMPLATE = "ratelimit:{scope}:{user_id}"

    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._script = self._redis.register_script(_LUA_TOKEN_BUCKET)

    async def consume(
        self,
        *,
        scope: str,
        user_id: int,
        capacity: int,
        refill_per_sec: float,
        cost: int = 1,
    ) -> RateLimitResult:
        import time

        key = self.KEY_TEMPLATE.format(scope=scope, user_id=user_id)
        now_ms = int(time.time() * 1000)
        result = await self._script(
            keys=[key],
            args=[capacity, refill_per_sec, now_ms, cost],
        )
        allowed, remaining, retry = result
        return RateLimitResult(
            allowed=bool(int(allowed)),
            remaining=float(remaining),
            retry_after_ms=int(retry),
        )
