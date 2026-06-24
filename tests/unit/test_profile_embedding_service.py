"""ProfileEmbeddingService 단위 테스트."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.exceptions import LLMClientError, ValidationError
from app.domain.models import ProfileEmbedding
from app.service.profile_embedding_service import ProfileEmbeddingService, _sha256

_FAKE_EMBEDDING = [0.1] * 768


_PROFILE_TEXT = "관심 카테고리: FOOD 활동 목적: 맛집 소개 선호 캠페인: VISIT 활동 수준: ACTIVE"


def _make_service(existing_record=None):
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=existing_record)
    llm = AsyncMock()
    svc = object.__new__(ProfileEmbeddingService)
    svc._session = session
    svc._llm = llm
    return svc


@pytest.mark.asyncio
async def test_embed_and_store_success():
    svc = _make_service()  # existing_record=None → 신규 저장
    svc._llm.embed = AsyncMock(return_value=[_FAKE_EMBEDDING])

    async def fake_flush():
        pass

    async def fake_refresh(obj):
        obj.id = 1

    svc._session.flush = fake_flush
    svc._session.refresh = fake_refresh

    result = await svc.embed_and_store(user_id=42, profile_text=_PROFILE_TEXT)

    svc._llm.embed.assert_awaited_once_with([_PROFILE_TEXT])
    svc._session.add.assert_called_once()

    added = svc._session.add.call_args[0][0]
    assert isinstance(added, ProfileEmbedding)
    assert added.user_id == 42
    assert added.embedding == _FAKE_EMBEDDING
    assert added.profile_hash == _sha256(_PROFILE_TEXT)


@pytest.mark.asyncio
async def test_embed_and_store_rejects_short_text():
    svc = _make_service()
    with pytest.raises(ValidationError):
        await svc.embed_and_store(user_id=1, profile_text="짧은 텍스트")


@pytest.mark.asyncio
async def test_embed_and_store_rejects_whitespace_only():
    svc = _make_service()
    with pytest.raises(ValidationError):
        await svc.embed_and_store(user_id=1, profile_text="   " * 10)


@pytest.mark.asyncio
async def test_embed_and_store_llm_failure_raises_llm_error():
    svc = _make_service()
    svc._llm.embed = AsyncMock(side_effect=Exception("OpenAI timeout"))
    with pytest.raises(LLMClientError):
        await svc.embed_and_store(user_id=1, profile_text=_PROFILE_TEXT)


@pytest.mark.asyncio
async def test_embed_and_store_empty_embedding_raises_llm_error():
    svc = _make_service()
    svc._llm.embed = AsyncMock(return_value=[])
    with pytest.raises(LLMClientError):
        await svc.embed_and_store(user_id=1, profile_text=_PROFILE_TEXT)


@pytest.mark.asyncio
async def test_embed_and_store_dedup_returns_existing():
    """동일 profile_hash가 이미 존재하면 LLM 호출 없이 기존 row를 반환한다."""
    cached = ProfileEmbedding(user_id=7, embedding=_FAKE_EMBEDDING, profile_hash=_sha256(_PROFILE_TEXT))
    cached.id = 99
    svc = _make_service(existing_record=cached)

    result = await svc.embed_and_store(user_id=7, profile_text=_PROFILE_TEXT)

    svc._llm.embed.assert_not_awaited()
    svc._session.add.assert_not_called()
    assert result is cached
