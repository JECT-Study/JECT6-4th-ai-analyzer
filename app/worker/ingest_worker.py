from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import time

import aio_pika

from app.client.llm_client import get_llm_client
from app.client.redis_client import get_redis
from app.core.config import get_settings
from app.core.database import session_scope
from app.core.logging import get_logger, setup_logging
from app.domain.enums import SourceType
from app.domain.schemas import ChunkRequest
from app.repository.crawl_queue import CrawlMessage, CrawlQueue
from app.repository import influencer_repository
from app.service.blog_snapshot_service import BlogSnapshotService
from app.service.document_service import DocumentService

logger = get_logger(__name__)

_BATCH_TTL           = 86400  # 24시간
_BATCH_DEADLINE_SEC  = 1800   # 30분 — 일부 실패 시 부분 스냅샷 트리거 기준

_SNAPSHOT_CREATING  = "creating"
_SNAPSHOT_COMPLETED = "completed"
_SNAPSHOT_FAILED    = "failed"

# failed → creating 전환을 원자적으로 수행하는 Lua 스크립트
# KEYS[1]: snapshot_status 키
# ARGV[1]: 현재 기대값 ("failed"), ARGV[2]: 전환값 ("creating"), ARGV[3]: TTL(초)
_TRANSITION_TO_CREATING_LUA = """
local curr = redis.call("GET", KEYS[1])
if curr == ARGV[1] then
    redis.call("SET", KEYS[1], ARGV[2], "EX", tonumber(ARGV[3]))
    return 1
end
return 0
"""


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
        correlation_id = fields.get("correlation_id") or None
        blog_id = fields.get("blog_id") or None
        analysis_mode = fields.get("analysis_mode") or "POST"
        batch_id = fields.get("batch_id") or None
        expected_count_raw = fields.get("expected_count") or None
        expected_count = int(expected_count_raw) if expected_count_raw else None

        try:
            source_type_raw = fields.get("source_type")
            if not source_type_raw:
                raise KeyError("source_type")
            metadata: dict = {}
            if blog_id:
                metadata["blog_id"] = blog_id
            if correlation_id:
                metadata["correlation_id"] = correlation_id
            if fields.get("url"):
                metadata["post_url"] = fields["url"]
            # ext_blog 전용 메타데이터: 인플루언서 식별 및 RAG 필터링에 활용
            if source_type_raw == "ext_blog":
                for key in ("nickname", "category", "source_blog_url"):
                    value = fields.get(key)
                    if value:
                        metadata[key] = value
            chunk_request = ChunkRequest(
                user_id=int(fields["user_id"]),
                source_type=SourceType(source_type_raw),
                title=(fields.get("title") or fields.get("url") or "")[:512],
                content=fields["content"],
                url=fields.get("url") or None,
                external_id=fields.get("external_id") or None,
                metadata=metadata,
            )
        except (KeyError, ValueError) as exc:
            logger.warning("invalid ingest message id=%s err=%s", message.id, exc)
            await self._queue.send_to_dlq(message, error_message=str(exc))
            await self._queue.ack(message.id)
            return

        # ext_blog: 임베딩 성공 여부와 무관하게 인플루언서 프로필 먼저 저장
        if chunk_request.source_type == SourceType.EXT_BLOG:
            source_blog_url = metadata.get("source_blog_url")
            if source_blog_url:
                try:
                    nickname = metadata.get("nickname")
                    category = metadata.get("category")
                    async with session_scope() as inf_session:
                        await influencer_repository.upsert_influencer(
                            inf_session,
                            blog_url=source_blog_url,
                            influencer_name=nickname,
                            blog_name=nickname,
                            title=chunk_request.title,
                            thumbnail_url=None,
                            category=category,
                        )
                except Exception as exc:
                    logger.warning("influencer upsert failed blog_url=%s err=%s", source_blog_url, exc)

        try:
            async with session_scope() as session:
                service = DocumentService(session, self._llm_client)
                response = await service.ingest_and_chunk(chunk_request)
            await self._queue.ack(message.id)
            logger.info(
                "ingest processed id=%s document_id=%s chunks=%s mode=%s",
                message.id,
                response.document_id,
                response.chunk_count,
                analysis_mode,
            )

            # 분석 모드에 따라 분기
            if analysis_mode == "FULL_BLOG" and batch_id and expected_count:
                # FULL_BLOG에서는 포스트마다 분석을 발행하지 않는다.
                # 같은 batch_id로 들어온 N개 포스트가 모두 ingest될 때까지 기다렸다가
                # BLOG_SNAPSHOT 문서 1건을 만든 뒤 분석 메시지를 정확히 1번만 발행한다.
                await self._handle_full_blog_batch(
                    batch_id=batch_id,
                    document_id=response.document_id,
                    user_id=chunk_request.user_id,
                    blog_id=blog_id,
                    correlation_id=correlation_id,
                    expected_count=expected_count,
                )
            elif correlation_id:
                # POST mode: 포스트별 즉시 분석 발행
                await self._mark_ingest_done(
                    correlation_id, response.document_id, chunk_request.user_id, "POST"
                )
        except Exception as exc:
            await self._handle_failure(message, exc=exc)

    async def _handle_full_blog_batch(
        self,
        *,
        batch_id: str,
        document_id: int,
        user_id: int,
        blog_id: str | None,
        correlation_id: str | None,
        expected_count: int,
    ) -> None:
        """FULL_BLOG 배치 진행 상황을 Redis에 추적하고, 완료 시 스냅샷을 생성해 분석 1회만 발행."""
        redis = self._queue._redis
        prefix = f"batch:{batch_id}"

        # batch는 스케줄러/야간 배치 작업이 아니라,
        # "한 번의 FULL_BLOG 분석 요청으로 생성된 여러 포스트 ingest 묶음"이다.
        # 이름은 batch_id지만 의미상 full_blog_request_group_id에 가깝다.
        # 각 포스트 worker가 같은 prefix 아래에 진행 상황을 기록하므로,
        # 마지막 포스트가 끝난 시점을 Redis 카운터로 판단할 수 있다.
        # 배치 메타데이터 저장 (멱등 — 모든 포스트가 동일한 값을 씀)
        await redis.set(f"{prefix}:user_id", str(user_id), ex=_BATCH_TTL)
        await redis.set(f"{prefix}:expected", str(expected_count), ex=_BATCH_TTL)
        if blog_id:
            await redis.set(f"{prefix}:blog_id", blog_id, ex=_BATCH_TTL)
        if correlation_id:
            await redis.set(f"{prefix}:correlation_id", correlation_id, ex=_BATCH_TTL)

        # deadline 설정 (최초 ingest 도착 시 1회만 — NX)
        deadline_key = f"{prefix}:deadline"
        await redis.set(
            deadline_key,
            str(int(time.time()) + _BATCH_DEADLINE_SEC),
            nx=True, ex=_BATCH_TTL,
        )

        # SADD: 동일 doc_id 재시도 시 중복 적재 방지 (RPUSH 대체)
        await redis.sadd(f"{prefix}:doc_ids", str(document_id))
        await redis.expire(f"{prefix}:doc_ids", _BATCH_TTL)

        # 원자적 카운터 증가. 병렬 worker가 처리해도 completed 값은 충돌 없이 증가한다.
        completed = await redis.incr(f"{prefix}:completed")
        await redis.expire(f"{prefix}:completed", _BATCH_TTL)

        # deadline 초과 여부 확인 (부분 ingest 실패 시 30분 후 부분 스냅샷 트리거)
        deadline_raw = await redis.get(deadline_key)
        past_deadline = bool(deadline_raw) and int(deadline_raw) < int(time.time())

        logger.info(
            "batch progress batch_id=%s completed=%s expected=%s past_deadline=%s",
            batch_id, completed, expected_count, past_deadline,
        )

        if completed >= expected_count or (past_deadline and completed > 0):
            if completed < expected_count:
                logger.warning(
                    "batch deadline exceeded, partial snapshot batch_id=%s completed=%s expected=%s",
                    batch_id, completed, expected_count,
                )
            status_key = f"{prefix}:snapshot_status"
            # 중복 진입 방지: NX 성공 워커만 진행
            locked = await redis.set(status_key, _SNAPSHOT_CREATING, nx=True, ex=_BATCH_TTL)
            if not locked:
                existing = (await redis.get(status_key)) or ""
                if existing in (_SNAPSHOT_COMPLETED, _SNAPSHOT_CREATING):
                    return  # 이미 완료됐거나 다른 워커가 진행 중
                # failed → creating 원자적 전환 (Lua): 동시 재진입 시 1개 워커만 선점
                retried = await redis.eval(
                    _TRANSITION_TO_CREATING_LUA,
                    1, status_key,
                    _SNAPSHOT_FAILED, _SNAPSHOT_CREATING, str(_BATCH_TTL),
                )
                if not int(retried):
                    return  # 다른 워커가 이미 선점
            await self._create_snapshot_and_publish(
                batch_id=batch_id, prefix=prefix, status_key=status_key
            )

    async def _create_snapshot_and_publish(
        self, *, batch_id: str, prefix: str, status_key: str
    ) -> None:
        """모든 포스트 ingest 완료 후 BLOG_SNAPSHOT 문서를 생성하고 분석 1회 발행."""
        redis = self._queue._redis
        try:
            raw_ids = await redis.smembers(f"{prefix}:doc_ids")
            doc_ids = [int(d) for d in raw_ids]
            user_id = int(await redis.get(f"{prefix}:user_id") or 0)
            blog_id_raw = await redis.get(f"{prefix}:blog_id")
            blog_id = int(blog_id_raw) if blog_id_raw else None
            correlation_id = await redis.get(f"{prefix}:correlation_id")

            async with session_scope() as session:
                snapshot_service = BlogSnapshotService(session, self._llm_client)
                snapshot_doc_id, source_doc_ids = await snapshot_service.create_snapshot(
                    user_id=user_id,
                    blog_id=blog_id,
                    batch_id=batch_id,
                    document_ids=doc_ids,
                    correlation_id=correlation_id,
                )

            await self._publish_analysis(
                user_id=user_id,
                document_id=snapshot_doc_id,
                correlation_id=correlation_id,
                analysis_mode="FULL_BLOG",
                batch_id=batch_id,
                source_document_ids=source_doc_ids,
            )
            await redis.set(status_key, _SNAPSHOT_COMPLETED, ex=_BATCH_TTL)
            logger.info(
                "full_blog snapshot analysis triggered batch_id=%s snapshot_doc_id=%s post_count=%s",
                batch_id, snapshot_doc_id, len(doc_ids),
            )
        except Exception as exc:
            await redis.set(status_key, _SNAPSHOT_FAILED, ex=_BATCH_TTL)
            logger.exception(
                "snapshot creation failed batch_id=%s err=%s", batch_id, exc
            )
            raise

    async def _mark_ingest_done(
        self, correlation_id: str, document_id: int, user_id: int, analysis_mode: str = "POST"
    ) -> None:
        """POST 모드 ingest 완료 신호를 Redis에 저장하고 blog.analysis 큐에 분석 메시지 발행."""
        try:
            redis = self._queue._redis
            key = f"ingest:done:{correlation_id}"
            await redis.set(key, str(document_id), ex=3600)
            logger.info(
                "ingest completion marked correlation_id=%s document_id=%s",
                correlation_id, document_id,
            )
        except Exception as exc:
            logger.warning(
                "ingest completion mark failed correlation_id=%s err=%s", correlation_id, exc
            )

        await self._publish_analysis(
            user_id=user_id,
            document_id=document_id,
            correlation_id=correlation_id,
            analysis_mode=analysis_mode,
        )

    async def _publish_analysis(
        self,
        *,
        user_id: int,
        document_id: int,
        correlation_id: str | None,
        analysis_mode: str,
        batch_id: str | None = None,
        source_document_ids: list[int] | None = None,
    ) -> None:
        """blog.analysis RabbitMQ 큐에 분석 메시지를 발행."""
        try:
            payload: dict = {
                "user_id": user_id,
                "document_id": document_id,
                "analysis_mode": analysis_mode,
            }
            if correlation_id:
                payload["correlation_id"] = correlation_id
            if batch_id:
                payload["batch_id"] = batch_id
            if source_document_ids:
                payload["source_document_ids"] = source_document_ids

            connection = await aio_pika.connect_robust(self._settings.rabbitmq_url)
            async with connection:
                channel = await connection.channel()
                await channel.default_exchange.publish(
                    aio_pika.Message(
                        body=json.dumps(payload).encode(),
                        content_type="application/json",
                        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    ),
                    routing_key=self._settings.analysis_queue_name,
                )
            logger.info(
                "analysis published mode=%s document_id=%s correlation_id=%s",
                analysis_mode, document_id, correlation_id,
            )
        except Exception as exc:
            logger.warning(
                "analysis publish failed mode=%s document_id=%s err=%s",
                analysis_mode, document_id, exc,
            )
            raise

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
