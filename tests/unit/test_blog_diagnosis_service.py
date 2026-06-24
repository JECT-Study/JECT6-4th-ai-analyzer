"""Unit tests for BlogDiagnosisService (E-4)."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.exceptions import NotFoundError
from app.service.blog_diagnosis_service import BlogDiagnosisService


# ── Fixture helpers ────────────────────────────────────────────────────────────

def _make_document(document_id: int = 1, user_id: int = 10, meta: dict | None = None):
    doc = MagicMock()
    doc.id = document_id
    doc.user_id = user_id
    doc.content = "블로그 본문 샘플 텍스트"
    doc.doc_metadata = meta or {}
    return doc


def _make_job(job_id: int = 99, result: dict | None = None):
    job = MagicMock()
    job.id = job_id
    job.result = result or {"summary": "테스트 요약"}
    return job


def _make_service(session=None, llm=None, demo_mode: bool = True):
    session = session or AsyncMock()
    llm = llm or AsyncMock()
    with patch("app.service.blog_diagnosis_service.get_settings") as mock_cfg:
        mock_cfg.return_value = MagicMock(demo_mode=demo_mode)
        svc = BlogDiagnosisService(session=session, llm_client=llm)
    svc._settings = MagicMock(demo_mode=demo_mode)
    return svc


# ── Tests ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_diagnose_not_found_raises():
    """문서가 없거나 소유자가 다르면 NotFoundError."""
    svc = _make_service()
    svc._documents = AsyncMock()
    svc._documents.get_by_id = AsyncMock(return_value=None)

    with pytest.raises(NotFoundError):
        await svc.diagnose(user_id=10, document_id=999)


@pytest.mark.asyncio
async def test_diagnose_wrong_owner_raises():
    """다른 user_id 문서는 NotFoundError."""
    svc = _make_service()
    svc._documents = AsyncMock()
    svc._documents.get_by_id = AsyncMock(return_value=_make_document(user_id=99))
    svc._jobs = AsyncMock()
    svc._jobs.get_latest_by_document = AsyncMock(return_value=None)

    with pytest.raises(NotFoundError):
        await svc.diagnose(user_id=10, document_id=1)


@pytest.mark.asyncio
async def test_diagnose_demo_mode_returns_diagnosis():
    """demo_mode=True 시 LLM 없이 진단 객체 반환."""
    svc = _make_service(demo_mode=True)
    svc._documents = AsyncMock()
    svc._documents.get_by_id = AsyncMock(return_value=_make_document())
    svc._jobs = AsyncMock()
    svc._jobs.get_latest_by_document = AsyncMock(return_value=_make_job())
    svc._llm.embed = AsyncMock(return_value=[[0.1] * 768])

    mock_diag = MagicMock()
    mock_diag.id = 1
    mock_diag.user_id = 10
    svc._session.add = MagicMock()
    svc._session.flush = AsyncMock()
    svc._session.refresh = AsyncMock()

    result = await svc.diagnose(user_id=10, document_id=1)

    svc._session.add.assert_called_once()
    svc._session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_aggregate_metrics_with_meta():
    """메타데이터가 있을 때 interaction/image/view 점수 산출."""
    svc = _make_service()
    meta = {
        "like_count": 50,
        "comment_count": 20,
        "image_count": 5,
        "view_count": 3000,
        "post_count": 10,
    }
    agg = svc._aggregate_metrics(meta, {})

    assert agg["interaction_score"] is not None
    assert agg["image_score"] == min(100, 5 * 15)
    assert agg["image_confidence"] == "HIGH"
    assert agg["view_score"] is not None
    assert agg["view_confidence"] == "HIGH"


@pytest.mark.asyncio
async def test_aggregate_metrics_empty_meta():
    """메타데이터 없으면 점수는 None, confidence=LOW."""
    svc = _make_service()
    agg = svc._aggregate_metrics({}, {})

    assert agg["interaction_score"] is None
    assert agg["image_score"] is None
    assert agg["image_confidence"] == "LOW"
    assert agg["view_score"] is None
    assert agg["view_confidence"] == "LOW"


@pytest.mark.asyncio
async def test_build_result_embedding_failure_returns_none():
    """임베딩 실패 시 None 반환 (서비스 중단 없음)."""
    svc = _make_service()
    svc._llm.embed = AsyncMock(side_effect=Exception("embed error"))

    result = await svc._build_result_embedding({"strengths": ["a"], "weaknesses": ["b"]}, 1)
    assert result is None
