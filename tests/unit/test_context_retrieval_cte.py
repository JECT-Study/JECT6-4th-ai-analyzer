"""CTE 기반 컨텍스트 조회 단위 테스트.

find_my_blog_context / find_ext_blog_context / find_job_posting_context 세 메서드가
CTE 수정 이후에도 올바른 ContextChunk 리스트를 반환하는지 검증한다.

핵심 검증 포인트:
- 결과가 DB 반환 순서(score DESC)를 그대로 유지하는지 (Python 레이어가 순서를 바꾸지 않음)
- 빈 결과 처리
- 파라미터 바인딩 정확성
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.repository.context_retrieval_repository import ContextChunk, ContextRetrievalRepository


# ─── 공통 헬퍼 ────────────────────────────────────────────────────────────────

def _make_repo():
    mock_session = AsyncMock()
    return ContextRetrievalRepository(mock_session), mock_session


def _fake_row(document_id: int, title: str, score: float, source_type: str = "my_blog"):
    row = MagicMock()
    row.__getitem__ = lambda self, key: {
        "document_id": document_id,
        "title": title,
        "content_preview": f"preview_{document_id}",
        "source_type": source_type,
        "doc_metadata": {},
        "score": score,
    }[key]
    return row


def _make_execute_mock(session: AsyncMock, rows: list):
    mock_result = MagicMock()
    mock_result.mappings.return_value.all.return_value = rows
    session.execute = AsyncMock(return_value=mock_result)


# ─── find_my_blog_context ─────────────────────────────────────────────────────

async def test_find_my_blog_context_returns_chunks_in_db_order():
    """DB가 score DESC 순서로 반환할 때 Python 레이어가 그 순서를 유지한다."""
    repo, session = _make_repo()
    rows = [
        _fake_row(10, "글 A", 0.95),
        _fake_row(3, "글 B", 0.88),   # id는 낮지만 score도 낮음 → 기존 쿼리라면 먼저 반환됐을 것
        _fake_row(7, "글 C", 0.71),
    ]
    _make_execute_mock(session, rows)

    chunks = await repo.find_my_blog_context(
        user_id=1, embedding=[0.1, 0.2], exclude_document_id=99, top_k=3
    )

    assert len(chunks) == 3
    assert chunks[0].document_id == 10
    assert chunks[0].score == pytest.approx(0.95)
    assert chunks[1].document_id == 3
    assert chunks[2].document_id == 7


async def test_find_my_blog_context_empty():
    """결과가 없을 때 빈 리스트를 반환한다."""
    repo, session = _make_repo()
    _make_execute_mock(session, [])

    chunks = await repo.find_my_blog_context(
        user_id=1, embedding=[0.0] * 768, exclude_document_id=1, top_k=5
    )
    assert chunks == []


async def test_find_my_blog_context_params():
    """user_id, exclude_doc, top_k 파라미터가 execute에 올바르게 전달된다."""
    repo, session = _make_repo()
    _make_execute_mock(session, [])

    await repo.find_my_blog_context(
        user_id=42, embedding=[0.1], exclude_document_id=7, top_k=5
    )

    session.execute.assert_called_once()
    _, bound = session.execute.call_args[0][1], session.execute.call_args[0][1]
    params = session.execute.call_args[0][1]
    assert params["user_id"] == 42
    assert params["exclude_doc"] == 7
    assert params["top_k"] == 5


# ─── find_ext_blog_context ────────────────────────────────────────────────────

async def test_find_ext_blog_context_returns_chunks_in_db_order():
    """DB가 score DESC 순서로 반환할 때 Python 레이어가 그 순서를 유지한다."""
    repo, session = _make_repo()
    rows = [
        _fake_row(50, "인플루언서 글 X", 0.93, "ext_blog"),
        _fake_row(2,  "인플루언서 글 Y", 0.80, "ext_blog"),  # id 낮음, 기존이면 우선됐을 것
        _fake_row(30, "인플루언서 글 Z", 0.67, "ext_blog"),
    ]
    _make_execute_mock(session, rows)

    chunks = await repo.find_ext_blog_context(embedding=[0.1, 0.2], top_k=3)

    assert len(chunks) == 3
    assert chunks[0].document_id == 50
    assert chunks[0].score == pytest.approx(0.93)
    assert chunks[1].document_id == 2
    assert chunks[2].document_id == 30


async def test_find_ext_blog_context_empty():
    repo, session = _make_repo()
    _make_execute_mock(session, [])
    assert await repo.find_ext_blog_context(embedding=[0.0], top_k=3) == []


async def test_find_ext_blog_context_params():
    repo, session = _make_repo()
    _make_execute_mock(session, [])

    emb = [0.1, 0.2, 0.3]
    await repo.find_ext_blog_context(embedding=emb, top_k=5)

    params = session.execute.call_args[0][1]
    assert params["embedding"] == str(emb)
    assert params["top_k"] == 5


# ─── find_job_posting_context ─────────────────────────────────────────────────

async def test_find_job_posting_context_returns_chunks_in_db_order():
    """DB가 score DESC 순서로 반환할 때 Python 레이어가 그 순서를 유지한다."""
    repo, session = _make_repo()
    rows = [
        _fake_row(100, "공고 A", 0.91, "job_posting"),
        _fake_row(1,   "공고 B", 0.75, "job_posting"),  # id=1은 가장 오래된 공고
        _fake_row(55,  "공고 C", 0.60, "job_posting"),
    ]
    _make_execute_mock(session, rows)

    chunks = await repo.find_job_posting_context(embedding=[0.1, 0.2], top_k=3)

    assert len(chunks) == 3
    assert chunks[0].document_id == 100
    assert chunks[0].score == pytest.approx(0.91)
    assert chunks[1].document_id == 1
    assert chunks[2].document_id == 55


async def test_find_job_posting_context_empty():
    repo, session = _make_repo()
    _make_execute_mock(session, [])
    assert await repo.find_job_posting_context(embedding=[0.0], top_k=5) == []


async def test_find_job_posting_context_params():
    repo, session = _make_repo()
    _make_execute_mock(session, [])

    emb = [0.5, 0.6]
    await repo.find_job_posting_context(embedding=emb, top_k=3)

    params = session.execute.call_args[0][1]
    assert params["embedding"] == str(emb)
    assert params["top_k"] == 3


# ─── 회귀 방지: 기존 낮은-id 우선 패턴 재발 감지 ─────────────────────────────

async def test_ext_blog_low_id_doc_not_artificially_prioritized():
    """id=1(가장 오래된 문서)이 score가 낮아도 우선 반환되지 않아야 한다.

    기존 버그: DISTINCT ON ... ORDER BY d.id LIMIT top_k → id 낮은 순으로 절단.
    CTE 수정 후: DB가 score DESC로 정렬해서 반환. Python은 그 순서 그대로 유지.
    이 테스트는 DB mock이 score DESC 순서로 row를 제공할 때 Python이 순서를 바꾸지 않음을 검증한다.
    """
    repo, session = _make_repo()
    # DB(CTE 이후)는 score 내림차순으로 반환한다고 가정
    rows = [
        _fake_row(999, "최신 고유사 글", 0.97, "ext_blog"),
        _fake_row(1,   "오래된 저유사 글", 0.20, "ext_blog"),
    ]
    _make_execute_mock(session, rows)

    chunks = await repo.find_ext_blog_context(embedding=[0.1], top_k=2)

    # score 높은 것이 첫 번째여야 한다
    assert chunks[0].document_id == 999
    assert chunks[0].score > chunks[1].score
