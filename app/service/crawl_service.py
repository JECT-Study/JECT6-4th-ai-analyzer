from app.client.redis_client import get_redis
from app.core.exceptions import RateLimitExceededError, ValidationError
from app.core.url_security import validate_crawl_destination
from app.domain.schemas import CrawlJobRequest, CrawlJobResponse
from app.repository.crawl_queue import CrawlQueue


class CrawlService:
    def __init__(self, queue: CrawlQueue | None = None) -> None:
        self._queue = queue or CrawlQueue(get_redis())

    async def enqueue(self, request: CrawlJobRequest) -> CrawlJobResponse:
        url = await validate_crawl_destination(request.url)
        retry_after_ms = await self._queue.retry_after_ms_for_domain(url)
        if retry_after_ms > 0:
            raise RateLimitExceededError(
                "crawl domain rate limit exceeded",
                retry_after_ms=retry_after_ms,
            )

        if not await self._queue.mark_url_seen(url):
            raise ValidationError("crawl url already queued or processed")

        try:
            job_id = await self._queue.enqueue(
                user_id=request.user_id,
                url=url,
                source_type=request.source_type.value,
                title=request.title,
                external_id=request.external_id,
                metadata=request.metadata,
            )
        except Exception:
            await self._queue.unmark_url_seen(url)
            raise

        return CrawlJobResponse(job_id=job_id, stream=self._queue.stream_name)
