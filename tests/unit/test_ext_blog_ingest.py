"""ext_blog ingest 메타데이터 매핑 단위 테스트.

IngestWorker._handle_message()가 source_type=ext_blog 메시지에서
nickname, category, source_blog_url을 ChunkRequest.metadata에 올바르게 매핑하는지 검증.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.worker.ingest_worker import IngestWorker
from app.repository.crawl_queue import CrawlMessage


def _make_worker():
    w = object.__new__(IngestWorker)
    queue_mock = AsyncMock()
    queue_mock._redis = AsyncMock()
    w._queue = queue_mock
    w._llm_client = AsyncMock()
    w._settings = MagicMock()
    w._settings.crawl_max_retries = 3
    w._settings.analysis_queue_name = "blog.analysis"
    w._settings.rabbitmq_url = "amqp://guest:guest@localhost/"
    return w


def _make_message(fields: dict) -> CrawlMessage:
    return CrawlMessage(id="1-0", fields=fields)


# ─── ext_blog 메타데이터 매핑 ────────────────────────────────────────────────

async def test_ext_blog_metadata_mapped_correctly():
    """ext_blog 메시지에서 nickname, category, source_blog_url이 메타데이터에 저장된다."""
    worker = _make_worker()
    captured: list[dict] = []

    async def fake_ingest(chunk_request):
        captured.append(dict(chunk_request.metadata))
        from app.domain.schemas import ChunkResponse
        return ChunkResponse(document_id=10, chunk_count=3)

    message = _make_message({
        "user_id": "0",
        "url": "https://blog.naver.com/influencer_a/123",
        "title": "뷰티 리뷰",
        "content": "상세한 리뷰 내용입니다.",
        "source_type": "ext_blog",
        "external_id": "https://blog.naver.com/influencer_a/123",
        "retry_count": "0",
        "nickname": "influencer_a",
        "category": "BEAUTY",
        "source_blog_url": "https://blog.naver.com/influencer_a",
    })

    with (
        patch("app.worker.ingest_worker.session_scope") as mock_scope,
        patch("app.worker.ingest_worker.DocumentService") as MockDocService,
    ):
        mock_session = AsyncMock()
        mock_scope.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_service = AsyncMock()
        mock_service.ingest_and_chunk = fake_ingest
        MockDocService.return_value = mock_service

        await worker._handle_message(message)

    assert len(captured) == 1
    meta = captured[0]
    assert meta["nickname"] == "influencer_a"
    assert meta["category"] == "BEAUTY"
    assert meta["source_blog_url"] == "https://blog.naver.com/influencer_a"
    assert meta["post_url"] == "https://blog.naver.com/influencer_a/123"


async def test_my_blog_metadata_no_ext_blog_fields():
    """my_blog 메시지에서는 nickname, category, source_blog_url이 메타데이터에 포함되지 않는다."""
    worker = _make_worker()
    captured: list[dict] = []

    async def fake_ingest(chunk_request):
        captured.append(dict(chunk_request.metadata))
        from app.domain.schemas import ChunkResponse
        return ChunkResponse(document_id=20, chunk_count=2)

    message = _make_message({
        "user_id": "1",
        "url": "https://blog.naver.com/user_a/456",
        "title": "내 블로그 글",
        "content": "본문입니다.",
        "source_type": "my_blog",
        "external_id": "https://blog.naver.com/user_a/456",
        "retry_count": "0",
        "blog_id": "42",
        "correlation_id": "corr-uuid",
    })

    with (
        patch("app.worker.ingest_worker.session_scope") as mock_scope,
        patch("app.worker.ingest_worker.DocumentService") as MockDocService,
    ):
        mock_session = AsyncMock()
        mock_scope.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_service = AsyncMock()
        mock_service.ingest_and_chunk = fake_ingest
        MockDocService.return_value = mock_service

        await worker._handle_message(message)

    assert len(captured) == 1
    meta = captured[0]
    assert "nickname" not in meta
    assert "category" not in meta
    assert "source_blog_url" not in meta
    assert meta.get("blog_id") == "42"
    assert meta.get("correlation_id") == "corr-uuid"


async def test_ext_blog_partial_metadata():
    """ext_blog 메시지에서 일부 메타데이터 필드만 있을 때 있는 것만 저장된다."""
    worker = _make_worker()
    captured: list[dict] = []

    async def fake_ingest(chunk_request):
        captured.append(dict(chunk_request.metadata))
        from app.domain.schemas import ChunkResponse
        return ChunkResponse(document_id=30, chunk_count=1)

    message = _make_message({
        "user_id": "0",
        "url": "https://blog.naver.com/x/1",
        "title": "글",
        "content": "본문",
        "source_type": "ext_blog",
        "external_id": "https://blog.naver.com/x/1",
        "retry_count": "0",
        "nickname": "x_blogger",
        # category, source_blog_url 없음
    })

    with (
        patch("app.worker.ingest_worker.session_scope") as mock_scope,
        patch("app.worker.ingest_worker.DocumentService") as MockDocService,
    ):
        mock_session = AsyncMock()
        mock_scope.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_scope.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_service = AsyncMock()
        mock_service.ingest_and_chunk = fake_ingest
        MockDocService.return_value = mock_service

        await worker._handle_message(message)

    meta = captured[0]
    assert meta["nickname"] == "x_blogger"
    assert "category" not in meta
    assert "source_blog_url" not in meta
