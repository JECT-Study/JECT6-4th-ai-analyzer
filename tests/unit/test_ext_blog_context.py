"""find_ext_blog_context() 단위 테스트.

ContextRetrievalRepository.find_ext_blog_context()가
pgvector 유사도 쿼리 결과를 올바른 ContextChunk 리스트로 변환하는지 검증.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.repository.context_retrieval_repository import ContextChunk, ContextRetrievalRepository


def _make_repo():
    mock_session = AsyncMock()
    return ContextRetrievalRepository(mock_session), mock_session


def _fake_row(document_id: int, title: str, preview: str, score: float):
    row = MagicMock()
    row.__getitem__ = lambda self, key: {
        "document_id": document_id,
        "title": title,
        "content_preview": preview,
        "source_type": "ext_blog",
        "doc_metadata": {},
        "score": score,
    }[key]
    return row


async def test_find_ext_blog_context_returns_chunks():
    """DB 결과를 ContextChunk 리스트로 올바르게 변환한다."""
    repo, mock_session = _make_repo()

    fake_rows = [
        _fake_row(1, "인플루언서 글 1", "미리보기 1", 0.92),
        _fake_row(2, "인플루언서 글 2", "미리보기 2", 0.85),
    ]
    mock_result = MagicMock()
    mock_result.mappings.return_value.all.return_value = fake_rows
    mock_session.execute = AsyncMock(return_value=mock_result)

    chunks = await repo.find_ext_blog_context(
        embedding=[0.1, 0.2, 0.3],
        top_k=3,
    )

    assert len(chunks) == 2
    assert chunks[0].document_id == 1
    assert chunks[0].title == "인플루언서 글 1"
    assert chunks[0].content_preview == "미리보기 1"
    assert chunks[0].source_type == "ext_blog"
    assert abs(chunks[0].score - 0.92) < 0.001


async def test_find_ext_blog_context_empty_result():
    """DB에 ext_blog 문서가 없으면 빈 리스트를 반환한다."""
    repo, mock_session = _make_repo()

    mock_result = MagicMock()
    mock_result.mappings.return_value.all.return_value = []
    mock_session.execute = AsyncMock(return_value=mock_result)

    chunks = await repo.find_ext_blog_context(
        embedding=[0.0] * 768,
        top_k=3,
    )

    assert chunks == []


async def test_find_ext_blog_context_passes_correct_query_params():
    """execute 호출 시 embedding 문자열과 top_k 파라미터가 올바르게 전달된다."""
    repo, mock_session = _make_repo()

    mock_result = MagicMock()
    mock_result.mappings.return_value.all.return_value = []
    mock_session.execute = AsyncMock(return_value=mock_result)

    embedding = [0.1, 0.2, 0.3]
    await repo.find_ext_blog_context(embedding=embedding, top_k=5)

    mock_session.execute.assert_called_once()
    _, params = mock_session.execute.call_args
    bound_params = mock_session.execute.call_args[0][1]
    assert str(embedding) in bound_params["embedding"] or bound_params["embedding"] == str(embedding)
    assert bound_params["top_k"] == 5
