import asyncio
import json
import signal

import aio_pika
from aio_pika.abc import AbstractIncomingMessage

from app.client.llm_client import get_llm_client
from app.core.config import get_settings
from app.core.database import session_scope
from app.core.logging import get_logger, setup_logging
from app.domain.schemas import AnalysisRequest
from app.service.analysis_service import AnalysisService

logger = get_logger(__name__)


class AnalysisWorker:
    """RabbitMQ에서 분석 이벤트를 받아 처리하는 워커.

    토폴로지:
        producer → blog.analysis (메인 큐, x-dead-letter-exchange=blog.analysis.dlx)
        실패 시 reject → DLX → blog.analysis.dlq

    재시도 정책:
        x-app-retry-count 헤더로 카운트. max_retries까지는 메인 큐로 republish,
        초과 시 reject(requeue=False) → DLQ에 적재(운영자 확인 후 수동 재처리).
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._stop_event = asyncio.Event()
        self._llm_client = get_llm_client()
        self._channel: aio_pika.abc.AbstractChannel | None = None

    async def run(self) -> None:
        connection = await aio_pika.connect_robust(self._settings.rabbitmq_url)
        async with connection:
            channel = await connection.channel()
            await channel.set_qos(prefetch_count=self._settings.worker_concurrency)
            self._channel = channel

            # DLX와 DLQ
            dlx = await channel.declare_exchange(
                self._settings.analysis_dlx_name,
                aio_pika.ExchangeType.FANOUT,
                durable=True,
            )
            dlq = await channel.declare_queue(
                self._settings.analysis_dlq_name, durable=True
            )
            await dlq.bind(dlx)

            # 메인 큐 (DLX 연결)
            main_queue = await channel.declare_queue(
                self._settings.analysis_queue_name,
                durable=True,
                arguments={
                    "x-dead-letter-exchange": self._settings.analysis_dlx_name,
                },
            )

            logger.info(
                "worker started queue=%s dlq=%s prefetch=%s max_retries=%s",
                self._settings.analysis_queue_name,
                self._settings.analysis_dlq_name,
                self._settings.worker_concurrency,
                self._settings.worker_max_retries,
            )

            await main_queue.consume(self._handle_message)
            await self._stop_event.wait()
            logger.info("worker stopping")

    async def stop(self) -> None:
        self._stop_event.set()

    async def _handle_message(self, message: AbstractIncomingMessage) -> None:
        # 메시지 파싱 실패는 즉시 DLQ로 (재시도 의미 없음)
        try:
            payload = json.loads(message.body)
            request = AnalysisRequest.model_validate(payload)
        except Exception as exc:
            logger.error("invalid message body, sending to DLQ: %s", exc)
            await message.reject(requeue=False)
            return

        try:
            async with session_scope() as session:
                service = AnalysisService(session, self._llm_client)
                await service.analyze(request)
            await message.ack()
            logger.info(
                "analysis processed user_id=%s document_id=%s",
                request.user_id,
                request.document_id,
            )
        except Exception as exc:
            await self._handle_failure(message, request, exc)

    async def _handle_failure(
        self,
        message: AbstractIncomingMessage,
        request: AnalysisRequest,
        exc: Exception,
    ) -> None:
        retry_count = self._read_retry_count(message)
        max_retries = self._settings.worker_max_retries

        if retry_count < max_retries:
            logger.warning(
                "analysis failed (retry %s/%s) user_id=%s document_id=%s err=%s",
                retry_count + 1,
                max_retries,
                request.user_id,
                request.document_id,
                exc,
            )
            await self._republish_with_retry(message, retry_count + 1)
            await message.ack()
        else:
            logger.exception(
                "analysis exhausted retries (%s) user_id=%s document_id=%s err=%s",
                max_retries,
                request.user_id,
                request.document_id,
                exc,
            )
            await message.reject(requeue=False)

    @staticmethod
    def _read_retry_count(message: AbstractIncomingMessage) -> int:
        headers = message.headers or {}
        value = headers.get("x-app-retry-count", 0)
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    async def _republish_with_retry(
        self, message: AbstractIncomingMessage, next_count: int
    ) -> None:
        assert self._channel is not None
        new_headers = dict(message.headers or {})
        new_headers["x-app-retry-count"] = next_count
        await self._channel.default_exchange.publish(
            aio_pika.Message(
                body=message.body,
                headers=new_headers,
                content_type=message.content_type,
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key=self._settings.analysis_queue_name,
        )


async def _main() -> None:
    setup_logging()
    worker = AnalysisWorker()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(worker.stop()))

    await worker.run()


if __name__ == "__main__":
    asyncio.run(_main())
