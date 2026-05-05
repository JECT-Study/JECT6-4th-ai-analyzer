import pytest

from app.core.exceptions import RateLimitExceededError, ValidationError
from app.domain.enums import SourceType
from app.domain.schemas import CrawlJobRequest
from app.service.crawl_service import CrawlService


class FakeCrawlQueue:
    stream_name = "crawl:jobs"

    def __init__(self, *, seen: bool = False, retry_after_ms: int = 0):
        self.seen = seen
        self.retry_after_ms = retry_after_ms
        self.unmarked = False
        self.enqueued = None

    async def retry_after_ms_for_domain(self, _url: str) -> int:
        return self.retry_after_ms

    async def mark_url_seen(self, _url: str) -> bool:
        return not self.seen

    async def unmark_url_seen(self, _url: str) -> None:
        self.unmarked = True

    async def enqueue(self, **kwargs) -> str:
        self.enqueued = kwargs
        return "1-0"


async def test_enqueue_validates_and_pushes_message(monkeypatch):
    async def fake_validate(url: str) -> str:
        return url

    monkeypatch.setattr("app.service.crawl_service.validate_crawl_destination", fake_validate)
    queue = FakeCrawlQueue()
    service = CrawlService(queue)

    response = await service.enqueue(
        CrawlJobRequest(
            user_id=1,
            url="https://example.com/post",
            source_type=SourceType.EXT_BLOG,
            metadata={"tag": "python"},
        )
    )

    assert response.job_id == "1-0"
    assert response.stream == "crawl:jobs"
    assert queue.enqueued["url"] == "https://example.com/post"
    assert queue.enqueued["metadata"] == {"tag": "python"}


async def test_enqueue_rejects_duplicate_url(monkeypatch):
    async def fake_validate(url: str) -> str:
        return url

    monkeypatch.setattr("app.service.crawl_service.validate_crawl_destination", fake_validate)
    service = CrawlService(FakeCrawlQueue(seen=True))

    with pytest.raises(ValidationError):
        await service.enqueue(CrawlJobRequest(user_id=1, url="https://example.com"))


async def test_enqueue_applies_domain_rate_limit(monkeypatch):
    async def fake_validate(url: str) -> str:
        return url

    monkeypatch.setattr("app.service.crawl_service.validate_crawl_destination", fake_validate)
    service = CrawlService(FakeCrawlQueue(retry_after_ms=500))

    with pytest.raises(RateLimitExceededError):
        await service.enqueue(CrawlJobRequest(user_id=1, url="https://example.com"))
