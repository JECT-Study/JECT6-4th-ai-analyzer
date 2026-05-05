from unittest.mock import AsyncMock

import pytest

from app.domain.enums import SourceType
from app.service.query_rewriter import QueryRewriter


@pytest.fixture
def llm_mock():
    mock = AsyncMock()
    mock.chat = AsyncMock(return_value="가상으로 작성된 본문 단락입니다.")
    return mock


class TestQueryRewriter:
    async def test_rewrites_job_posting(self, llm_mock):
        rewriter = QueryRewriter(llm_mock)
        result = await rewriter.rewrite(SourceType.JOB_POSTING, "백엔드 개발자 채용 (Spring, Kafka)")
        assert result == "가상으로 작성된 본문 단락입니다."
        llm_mock.chat.assert_awaited_once()

    async def test_rewrites_external_blog(self, llm_mock):
        rewriter = QueryRewriter(llm_mock)
        result = await rewriter.rewrite(SourceType.EXT_BLOG, "Kubernetes 운영 경험기")
        assert result == "가상으로 작성된 본문 단락입니다."

    async def test_skips_my_blog(self, llm_mock):
        rewriter = QueryRewriter(llm_mock)
        original = "내 블로그 글 내용"
        result = await rewriter.rewrite(SourceType.MY_BLOG, original)
        assert result == original
        llm_mock.chat.assert_not_awaited()

    async def test_falls_back_to_original_on_llm_error(self):
        llm_mock = AsyncMock()
        llm_mock.chat = AsyncMock(side_effect=RuntimeError("LLM down"))
        rewriter = QueryRewriter(llm_mock)
        original = "공고 텍스트"
        result = await rewriter.rewrite(SourceType.JOB_POSTING, original)
        assert result == original

    async def test_falls_back_when_llm_returns_empty(self):
        llm_mock = AsyncMock()
        llm_mock.chat = AsyncMock(return_value="   ")
        rewriter = QueryRewriter(llm_mock)
        original = "공고 텍스트"
        result = await rewriter.rewrite(SourceType.JOB_POSTING, original)
        assert result == original
