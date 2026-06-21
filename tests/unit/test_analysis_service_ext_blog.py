"""AnalysisService RAG ext_blog 컨텍스트 포함 단위 테스트.

_build_rag_context()가 ext_blog 청크를 포함한 컨텍스트 문자열을 생성하는지 검증.
비동기 응답 계약 및 프롬프트 구조도 검증한다.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.repository.context_retrieval_repository import ContextChunk
from app.service.analysis_service import AnalysisService, _RAG_CONTEXT_TEMPLATE


def _make_service():
    mock_session = AsyncMock()
    mock_llm = AsyncMock()
    svc = object.__new__(AnalysisService)
    svc._session = mock_session
    svc._llm = mock_llm
    svc._jobs = AsyncMock()
    svc._documents = AsyncMock()
    svc._context = AsyncMock()
    svc._rate_limiter = AsyncMock()
    svc._settings = MagicMock()
    svc._settings.demo_mode = False
    svc._settings.llm_provider = "openai"
    return svc


def _make_chunk(doc_id: int, title: str, preview: str, source_type: str = "ext_blog") -> ContextChunk:
    return ContextChunk(
        document_id=doc_id,
        title=title,
        content_preview=preview,
        source_type=source_type,
        score=0.9,
    )


# ─── _build_rag_context 포함 테스트 ─────────────────────────────────────────────

async def test_build_rag_context_includes_ext_blog():
    """_build_rag_context가 ext_blog 청크를 '유사 인플루언서 블로그 글' 섹션에 포함한다."""
    svc = _make_service()
    svc._context.get_document_avg_embedding = AsyncMock(return_value=[0.1, 0.2])
    svc._context.find_my_blog_context = AsyncMock(return_value=[
        _make_chunk(1, "내 블로그 글", "내 이전 글 미리보기", source_type="my_blog"),
    ])
    svc._context.find_ext_blog_context = AsyncMock(return_value=[
        _make_chunk(10, "인플루언서 글 A", "인플루언서 미리보기 A"),
    ])
    svc._context.find_job_posting_context = AsyncMock(return_value=[
        _make_chunk(20, "광고 캠페인 1", "캠페인 미리보기", source_type="job_posting"),
    ])

    context = await svc._build_rag_context(document_id=5, user_id=1)

    assert context is not None
    assert "인플루언서 글 A" in context
    assert "인플루언서 미리보기 A" in context
    assert "유사 인플루언서 블로그 글" in context
    assert "내 블로그 글" in context
    assert "광고 캠페인 1" in context


async def test_build_rag_context_ext_blog_find_called():
    """find_ext_blog_context가 반드시 호출된다."""
    svc = _make_service()
    svc._context.get_document_avg_embedding = AsyncMock(return_value=[0.1])
    svc._context.find_my_blog_context = AsyncMock(return_value=[])
    svc._context.find_ext_blog_context = AsyncMock(return_value=[])
    svc._context.find_job_posting_context = AsyncMock(return_value=[])

    await svc._build_rag_context(document_id=5, user_id=1)

    svc._context.find_ext_blog_context.assert_called_once()
    call_kwargs = svc._context.find_ext_blog_context.call_args.kwargs
    assert "embedding" in call_kwargs
    assert call_kwargs["top_k"] == 3


async def test_build_rag_context_no_data_returns_none():
    """모든 소스에서 청크가 없으면 None을 반환한다."""
    svc = _make_service()
    svc._context.get_document_avg_embedding = AsyncMock(return_value=[0.1])
    svc._context.find_my_blog_context = AsyncMock(return_value=[])
    svc._context.find_ext_blog_context = AsyncMock(return_value=[])
    svc._context.find_job_posting_context = AsyncMock(return_value=[])

    result = await svc._build_rag_context(document_id=5, user_id=1)

    assert result is None


async def test_build_rag_context_only_ext_blog_present():
    """ext_blog 청크만 있어도 컨텍스트가 생성된다 (my_blog, job_posting 없어도)."""
    svc = _make_service()
    svc._context.get_document_avg_embedding = AsyncMock(return_value=[0.1])
    svc._context.find_my_blog_context = AsyncMock(return_value=[])
    svc._context.find_ext_blog_context = AsyncMock(return_value=[
        _make_chunk(10, "인플루언서 유일 글", "유일 미리보기"),
    ])
    svc._context.find_job_posting_context = AsyncMock(return_value=[])

    context = await svc._build_rag_context(document_id=5, user_id=1)

    assert context is not None
    assert "인플루언서 유일 글" in context


async def test_build_rag_context_fallback_text_when_empty():
    """ext_blog 청크가 없으면 '관련 인플루언서 글 없음' 텍스트가 포함된다."""
    svc = _make_service()
    svc._context.get_document_avg_embedding = AsyncMock(return_value=[0.1])
    svc._context.find_my_blog_context = AsyncMock(return_value=[
        _make_chunk(1, "내 글", "미리보기", source_type="my_blog"),
    ])
    svc._context.find_ext_blog_context = AsyncMock(return_value=[])
    svc._context.find_job_posting_context = AsyncMock(return_value=[])

    context = await svc._build_rag_context(document_id=5, user_id=1)

    assert context is not None
    assert "관련 인플루언서 글 없음" in context


# ─── RAG 템플릿 구조 검증 ─────────────────────────────────────────────────────

def test_rag_context_template_has_influencer_section():
    """_RAG_CONTEXT_TEMPLATE에 인플루언서 섹션 플레이스홀더가 존재한다."""
    assert "{influencer_context}" in _RAG_CONTEXT_TEMPLATE
    assert "{blog_context}" in _RAG_CONTEXT_TEMPLATE
    assert "{campaign_context}" in _RAG_CONTEXT_TEMPLATE
    assert "유사 인플루언서 블로그 글" in _RAG_CONTEXT_TEMPLATE


def test_rag_context_template_section_order():
    """인플루언서 섹션이 blog 섹션과 campaign 섹션 사이에 위치한다."""
    blog_pos = _RAG_CONTEXT_TEMPLATE.index("{blog_context}")
    influencer_pos = _RAG_CONTEXT_TEMPLATE.index("{influencer_context}")
    campaign_pos = _RAG_CONTEXT_TEMPLATE.index("{campaign_context}")
    assert blog_pos < influencer_pos < campaign_pos
