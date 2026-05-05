import asyncio
import json
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from app.core.config import get_settings


@dataclass(frozen=True)
class CrawlMessage:
    id: str
    fields: dict[str, str]


class CrawlQueue:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._settings = get_settings()

    @property
    def stream_name(self) -> str:
        return self._settings.crawl_stream_name

    async def enqueue(
        self,
        *,
        user_id: int,
        url: str,
        source_type: str,
        title: str | None,
        external_id: str | None,
        metadata: dict,
        retry_count: int = 0,
    ) -> str:
        payload = {
            "user_id": str(user_id),
            "url": url,
            "source_type": source_type,
            "title": title or "",
            "external_id": external_id or "",
            "metadata": json.dumps(metadata, ensure_ascii=False),
            "retry_count": str(retry_count),
        }
        return await self._redis.xadd(self._settings.crawl_stream_name, payload)

    async def mark_url_seen(self, url: str) -> bool:
        added = await self._redis.sadd("crawl:seen:urls", url)
        return bool(added)

    async def unmark_url_seen(self, url: str) -> None:
        await self._redis.srem("crawl:seen:urls", url)

    async def retry_after_ms_for_domain(self, url: str) -> int:
        domain = urlparse(url).hostname or ""
        if not domain:
            return 0
        key = "crawl:ratelimit:domain"
        now = time.time()
        last = await self._redis.hget(key, domain)
        if last is not None:
            elapsed = now - float(last)
            delay = self._settings.crawl_domain_delay_seconds
            if elapsed < delay:
                return int((delay - elapsed) * 1000)
        await self._redis.hset(key, domain, now)
        return 0

    async def wait_for_domain_slot(self, url: str) -> None:
        retry_after_ms = await self.retry_after_ms_for_domain(url)
        if retry_after_ms > 0:
            await asyncio.sleep(retry_after_ms / 1000)
            await self.retry_after_ms_for_domain(url)

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
        # redis-py returns (next_start_id, messages, deleted_ids)
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

    async def send_to_dlq(
        self, message: CrawlMessage, *, error_message: str
    ) -> str:
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
