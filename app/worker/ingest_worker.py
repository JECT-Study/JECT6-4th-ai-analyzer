import asyncio
import os
import signal
import socket

from app.client.llm_client import get_llm_client
from app.client.redis_client import get_redis
from app.core.config import get_settings
from app.core.database import session_scope
from app.core.logging import get_logger, setup_logging
from app.domain.enums import SourceType
from app.domain.schemas import ChunkRequest
from app.repository.crawl_queue import CrawlMessage, CrawlQueue
from app.service.document_service import DocumentService

logger = get_logger(__name__)


class IngestWorker:
    """ject_crawl이 Redis Stream에 publish한 본문을 읽어 청킹·임베딩·저장하는 워커.

    크롤링은 ject_crawl이 담당하므로 이 워커는 HTTP fetch 없이 content를 바로 처리한다.
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._stop_event = asyncio.Event()
        self._queue = CrawlQueue(get_redis())
        self._llm_client = get_llm_client()
        self._consumer_name = self._build_consumer_name()

    async def run(self) -> None:
        await self._queue.ensure_group()
        logger.info("ingest worker started consumer=%s", self._consumer_name)
        while not self._stop_event.is_set():
            claimed = await self._queue.claim_pending(self._consumer_name)
            messages = claimed or await self._queue.read(self._consumer_name)
            for message in messages:
                await self._handle_message(message)
        logger.info("ingest worker stopping consumer=%s", self._consumer_name)

    async def stop(self) -> None:
        self._stop_event.set()

    async def _handle_message(self, message: CrawlMessage) -> None:
        fields = message.fields
        try:
            chunk_request = ChunkRequest(
                user_id=int(fields["user_id"]),
                source_type=SourceType(fields["source_type"]),
                title=(fields.get("title") or fields.get("url") or "")[:512],
                content=fields["content"],
                url=fields.get("url") or None,
                external_id=fields.get("external_id") or None,
            )
        except (KeyError, ValueError) as exc:
            logger.warning("invalid ingest message id=%s err=%s", message.id, exc)
            await self._queue.send_to_dlq(message, error_message=str(exc))
            await self._queue.ack(message.id)
            return

        try:
            async with session_scope() as session:
                service = DocumentService(session, self._llm_client)
                response = await service.ingest_and_chunk(chunk_request)
            await self._queue.ack(message.id)
            logger.info(
                "ingest processed id=%s document_id=%s chunks=%s",
                message.id,
                response.document_id,
                response.chunk_count,
            )
        except Exception as exc:
            await self._handle_failure(message, exc=exc)

    async def _handle_failure(self, message: CrawlMessage, *, exc: Exception) -> None:
        retry_count = self._read_retry_count(message)
        if retry_count < self._settings.crawl_max_retries:
            fields = dict(message.fields)
            fields["retry_count"] = str(retry_count + 1)
            await self._queue.requeue(fields)
            await self._queue.ack(message.id)
            logger.warning(
                "ingest failed retry=%s/%s id=%s err=%s",
                retry_count + 1,
                self._settings.crawl_max_retries,
                message.id,
                exc.__class__.__name__,
            )
            return

        await self._queue.send_to_dlq(message, error_message=str(exc))
        await self._queue.ack(message.id)
        logger.exception(
            "ingest exhausted retries id=%s err=%s",
            message.id,
            exc.__class__.__name__,
        )

    @staticmethod
    def _read_retry_count(message: CrawlMessage) -> int:
        try:
            return int(message.fields.get("retry_count", "0"))
        except ValueError:
            return 0

    def _build_consumer_name(self) -> str:
        if self._settings.crawl_worker_name != "worker-1":
            return self._settings.crawl_worker_name
        return f"ingest-{socket.gethostname()}-{os.getpid()}"


async def _main() -> None:
    setup_logging()
    worker = IngestWorker()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(worker.stop()))

    await worker.run()


if __name__ == "__main__":
    asyncio.run(_main())
