import json

from redis.asyncio import Redis

from app.core.config import get_settings
from app.domain.schemas import ChatMessage


class ConversationRepository:
    """대화 세션을 Redis에 저장. stateless 서버 + 세션 분산 처리 용이."""

    SESSION_KEY = "chat:session:{session_id}"
    TOKEN_KEY = "chat:tokens:{session_id}"

    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._ttl = get_settings().conversation_ttl_seconds

    def _session_key(self, session_id: str) -> str:
        return self.SESSION_KEY.format(session_id=session_id)

    def _token_key(self, session_id: str) -> str:
        return self.TOKEN_KEY.format(session_id=session_id)

    async def get_messages(self, session_id: str) -> list[ChatMessage]:
        raw_list = await self._redis.lrange(self._session_key(session_id), 0, -1)
        return [ChatMessage.model_validate(json.loads(raw)) for raw in raw_list]

    async def append_message(self, session_id: str, message: ChatMessage) -> None:
        key = self._session_key(session_id)
        await self._redis.rpush(key, message.model_dump_json())
        await self._redis.expire(key, self._ttl)

    async def get_token_usage(self, session_id: str) -> int:
        value = await self._redis.get(self._token_key(session_id))
        return int(value) if value else 0

    async def add_token_usage(self, session_id: str, tokens: int) -> int:
        key = self._token_key(session_id)
        new_total = await self._redis.incrby(key, tokens)
        await self._redis.expire(key, self._ttl)
        return int(new_total)

    async def clear(self, session_id: str) -> None:
        await self._redis.delete(
            self._session_key(session_id), self._token_key(session_id)
        )
