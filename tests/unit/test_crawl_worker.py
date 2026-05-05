import json

from app.repository.crawl_queue import CrawlMessage
from app.worker.crawl_worker import CrawlWorker


def test_parse_message_builds_crawl_request():
    message = CrawlMessage(
        id="1-0",
        fields={
            "user_id": "7",
            "url": "https://example.com/post",
            "source_type": "ext_blog",
            "title": "제목",
            "external_id": "post-1",
            "metadata": json.dumps({"a": 1}),
            "retry_count": "0",
        },
    )

    request = CrawlWorker._parse_message(message)

    assert request.user_id == 7
    assert request.url == "https://example.com/post"
    assert request.title == "제목"
    assert request.metadata == {"a": 1}


def test_read_retry_count_defaults_to_zero():
    message = CrawlMessage(id="1-0", fields={"retry_count": "bad"})

    assert CrawlWorker._read_retry_count(message) == 0
