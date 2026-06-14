import asyncio
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from app.core.config import get_settings


@dataclass(frozen=True)
class CrawlMessage:
    id: str
    fields: dict[str, str]


class CrawlQueue:
    """ject_crawl이 publish한 ingest 메시지를 consume하는 큐."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._settings = get_settings()

    @property
    def stream_name(self) -> str:
        return self._settings.crawl_stream_name

    async def ensure_group(self) -> None:
        try:
            await self._redis.xgroup_create(
                self._settings.crawl_stream_name,
                self._settings.crawl_consumer_group,
                id="0",
                mkstream=True,
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def read(self, consumer_name: str) -> list[CrawlMessage]:
        messages = await self._redis.xreadgroup(
            self._settings.crawl_consumer_group,
            consumer_name,
            {self._settings.crawl_stream_name: ">"},
            count=self._settings.crawl_batch_size,
            block=self._settings.crawl_block_ms,
        )
        return self._decode_messages(messages)

    async def claim_pending(self, consumer_name: str) -> list[CrawlMessage]:
        result = await self._redis.xautoclaim(
            self._settings.crawl_stream_name,
            self._settings.crawl_consumer_group,
            consumer_name,
            min_idle_time=self._settings.crawl_pending_idle_ms,
            start_id="0",
            count=self._settings.crawl_batch_size,
        )
        messages = result[1] if len(result) > 1 else []
        return [
            CrawlMessage(id=message_id, fields=dict(fields))
            for message_id, fields in messages
        ]

    async def ack(self, message_id: str) -> None:
        await self._redis.xack(
            self._settings.crawl_stream_name,
            self._settings.crawl_consumer_group,
            message_id,
        )

    async def requeue(self, fields: dict[str, str]) -> str:
        """재시도용 메시지를 스트림에 다시 적재."""
        return await self._redis.xadd(self._settings.crawl_stream_name, fields)

    async def send_to_dlq(self, message: CrawlMessage, *, error_message: str) -> str:
        payload = dict(message.fields)
        payload["failed_message_id"] = message.id
        payload["error_message"] = error_message[:1000]
        return await self._redis.xadd(self._settings.crawl_dlq_stream_name, payload)

    @staticmethod
    def _decode_messages(raw_messages) -> list[CrawlMessage]:
        decoded: list[CrawlMessage] = []
        for _stream, messages in raw_messages or []:
            for message_id, fields in messages:
                decoded.append(CrawlMessage(id=message_id, fields=dict(fields)))
        return decoded
