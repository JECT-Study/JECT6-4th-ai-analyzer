import asyncio
import hashlib
import json
import os
import signal
import socket
from datetime import UTC, datetime
from urllib.parse import urlparse

from pydantic import ValidationError as PydanticValidationError

from app.client.crawler_client import CrawlerClient
from app.client.llm_client import get_llm_client
from app.client.redis_client import get_redis
from app.core.config import get_settings
from app.core.database import session_scope
from app.core.logging import get_logger, setup_logging
from app.domain.schemas import ChunkRequest, CrawlJobRequest
from app.repository.crawl_queue import CrawlMessage, CrawlQueue
from app.service.document_service import DocumentService
from app.service.html_extractor import HtmlExtractor

logger = get_logger(__name__)


class CrawlWorker:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._stop_event = asyncio.Event()
        self._queue = CrawlQueue(get_redis())
        self._crawler = CrawlerClient()
        self._extractor = HtmlExtractor()
        self._llm_client = get_llm_client()
        self._consumer_name = self._build_consumer_name()

    async def run(self) -> None:
        await self._queue.ensure_group()
        logger.info("crawl worker started consumer=%s", self._consumer_name)
        while not self._stop_event.is_set():
            claimed = await self._queue.claim_pending(self._consumer_name)
            messages = claimed or await self._queue.read(self._consumer_name)
            for message in messages:
                await self._handle_message(message)
        logger.info("crawl worker stopping consumer=%s", self._consumer_name)

    async def stop(self) -> None:
        self._stop_event.set()

    async def _handle_message(self, message: CrawlMessage) -> None:
        try:
            request = self._parse_message(message)
        except Exception as exc:
            logger.warning("invalid crawl message id=%s err=%s", message.id, exc)
            await self._queue.send_to_dlq(message, error_message=str(exc))
            await self._queue.ack(message.id)
            return

        domain = urlparse(request.url).hostname or "unknown"
        try:
            await self._queue.wait_for_domain_slot(request.url)
            page = await self._crawler.fetch(request.url)
            content = self._extractor.extract_text(page.html)
            title = request.title or self._extractor.extract_title(page.html) or request.url
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            metadata = dict(request.metadata)
            metadata["crawler"] = {
                "http_status": page.http_status,
                "content_type": page.content_type,
                "fetched_at": datetime.now(UTC).isoformat(),
                "content_hash": content_hash,
                "source": "crawl_worker",
            }

            chunk_request = ChunkRequest(
                user_id=request.user_id,
                source_type=request.source_type,
                title=title[:512],
                content=content,
                url=page.url,
                external_id=request.external_id,
                metadata=metadata,
                content_hash=content_hash,
                crawled_at=datetime.now(UTC),
                ingestion_status="completed",
            )

            async with session_scope() as session:
                service = DocumentService(session, self._llm_client)
                response = await service.ingest_and_chunk(chunk_request)

            await self._queue.ack(message.id)
            logger.info(
                "crawl processed id=%s domain=%s document_id=%s chunks=%s",
                message.id,
                domain,
                response.document_id,
                response.chunk_count,
            )
        except Exception as exc:
            await self._handle_failure(message, domain=domain, exc=exc)

    async def _handle_failure(
        self, message: CrawlMessage, *, domain: str, exc: Exception
    ) -> None:
        retry_count = self._read_retry_count(message)
        if retry_count < self._settings.crawl_max_retries:
            fields = dict(message.fields)
            fields["retry_count"] = str(retry_count + 1)
            await self._queue.enqueue(
                user_id=int(fields["user_id"]),
                url=fields["url"],
                source_type=fields["source_type"],
                title=fields.get("title") or None,
                external_id=fields.get("external_id") or None,
                metadata=json.loads(fields.get("metadata") or "{}"),
                retry_count=retry_count + 1,
            )
            await self._queue.ack(message.id)
            logger.warning(
                "crawl failed retry=%s/%s id=%s domain=%s err=%s",
                retry_count + 1,
                self._settings.crawl_max_retries,
                message.id,
                domain,
                exc.__class__.__name__,
            )
            return

        await self._queue.send_to_dlq(message, error_message=str(exc))
        await self._queue.ack(message.id)
        logger.exception(
            "crawl exhausted retries id=%s domain=%s err=%s",
            message.id,
            domain,
            exc.__class__.__name__,
        )

    @staticmethod
    def _parse_message(message: CrawlMessage) -> CrawlJobRequest:
        fields = message.fields
        metadata = json.loads(fields.get("metadata") or "{}")
        try:
            return CrawlJobRequest(
                user_id=int(fields["user_id"]),
                url=fields["url"],
                source_type=fields["source_type"],
                title=fields.get("title") or None,
                external_id=fields.get("external_id") or None,
                metadata=metadata,
            )
        except (KeyError, ValueError, PydanticValidationError) as exc:
            raise ValueError(f"invalid crawl message payload: {exc}") from exc

    @staticmethod
    def _read_retry_count(message: CrawlMessage) -> int:
        try:
            return int(message.fields.get("retry_count", "0"))
        except ValueError:
            return 0

    def _build_consumer_name(self) -> str:
        if self._settings.crawl_worker_name != "worker-1":
            return self._settings.crawl_worker_name
        return f"worker-{socket.gethostname()}-{os.getpid()}"


async def _main() -> None:
    setup_logging()
    worker = CrawlWorker()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(worker.stop()))

    await worker.run()


if __name__ == "__main__":
    asyncio.run(_main())
