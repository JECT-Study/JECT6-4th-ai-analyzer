"""임베딩 캐시.

청크 텍스트의 SHA256을 키로 해서 Redis에 임베딩을 저장.
동일 텍스트 재청킹 시 OpenAI 호출 비용 절감.
"""
import hashlib
import json
from collections.abc import Sequence

from redis.asyncio import Redis


class EmbeddingCache:
    KEY_TEMPLATE = "embed:{model}:{digest}"
    DEFAULT_TTL_SECONDS = 60 * 60 * 24 * 30  # 30일

    def __init__(
        self,
        redis: Redis,
        *,
        model: str,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._redis = redis
        self._model = model
        self._ttl = ttl_seconds

    @staticmethod
    def _hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _key(self, text: str) -> str:
        return self.KEY_TEMPLATE.format(model=self._model, digest=self._hash(text))

    async def get_many(
        self, texts: Sequence[str]
    ) -> tuple[list[list[float] | None], list[int]]:
        """캐시 조회. (각 인덱스의 임베딩 or None, 미스 인덱스 목록) 반환."""
        if not texts:
            return [], []
        keys = [self._key(t) for t in texts]
        raw = await self._redis.mget(keys)
        embeddings: list[list[float] | None] = []
        misses: list[int] = []
        for i, value in enumerate(raw):
            if value is None:
                embeddings.append(None)
                misses.append(i)
            else:
                embeddings.append(json.loads(value))
        return embeddings, misses

    async def set_many(
        self, texts: Sequence[str], embeddings: Sequence[list[float]]
    ) -> None:
        if not texts:
            return
        pipe = self._redis.pipeline()
        for text, emb in zip(texts, embeddings, strict=True):
            pipe.set(self._key(text), json.dumps(emb), ex=self._ttl)
        await pipe.execute()
