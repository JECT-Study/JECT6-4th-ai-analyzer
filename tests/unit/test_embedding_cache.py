import fakeredis.aioredis
import pytest

from app.repository.embedding_cache import EmbeddingCache


@pytest.fixture
async def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.flushall()
    await client.aclose()


@pytest.fixture
def cache(redis_client):
    return EmbeddingCache(redis_client, model="text-embedding-3-small")


class TestEmbeddingCache:
    async def test_get_many_returns_all_misses_when_empty(self, cache):
        cached, misses = await cache.get_many(["a", "b", "c"])
        assert cached == [None, None, None]
        assert misses == [0, 1, 2]

    async def test_get_many_handles_empty_input(self, cache):
        cached, misses = await cache.get_many([])
        assert cached == []
        assert misses == []

    async def test_set_and_get_round_trip(self, cache):
        texts = ["hello", "world"]
        embeddings = [[0.1, 0.2], [0.3, 0.4]]
        await cache.set_many(texts, embeddings)

        cached, misses = await cache.get_many(texts)
        assert misses == []
        assert cached == embeddings

    async def test_partial_hit(self, cache):
        await cache.set_many(["a", "c"], [[1.0], [3.0]])
        cached, misses = await cache.get_many(["a", "b", "c", "d"])
        assert cached[0] == [1.0]
        assert cached[1] is None
        assert cached[2] == [3.0]
        assert cached[3] is None
        assert misses == [1, 3]

    async def test_different_models_use_different_keys(self, redis_client):
        cache_a = EmbeddingCache(redis_client, model="model-a")
        cache_b = EmbeddingCache(redis_client, model="model-b")
        await cache_a.set_many(["x"], [[1.0]])

        cached_a, _ = await cache_a.get_many(["x"])
        cached_b, _ = await cache_b.get_many(["x"])
        assert cached_a == [[1.0]]
        assert cached_b == [None]

    async def test_identical_text_produces_same_key(self, cache):
        await cache.set_many(["same"], [[0.5]])
        cached, misses = await cache.get_many(["same"])
        assert cached == [[0.5]]
        assert misses == []
