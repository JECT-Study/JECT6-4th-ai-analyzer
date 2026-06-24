from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest

from app.core.exceptions import NotFoundError, TokenLimitExceededError
from app.core.rate_limiter import RateLimiter, RateLimitResult
from app.domain.enums import AnalysisStatus, ChatRole
from app.domain.schemas import ChatRequest
from app.repository.conversation_repository import ConversationRepository
from app.service.conversation_service import ConversationService


def _build_document(user_id=1, document_id=10, title="제목"):
    doc = MagicMock()
    doc.id = document_id
    doc.user_id = user_id
    doc.title = title
    return doc


def _build_completed_job():
    job = MagicMock()
    job.status = AnalysisStatus.COMPLETED
    job.result = {
        "summary": "요약",
        "key_topics": ["토픽1", "토픽2"],
        "tone": "친근함",
        "target_audience": "주니어 개발자",
        "suggestions": ["예시 추가"],
    }
    return job


@pytest.fixture
async def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.flushall()
    await client.aclose()


@pytest.fixture
def conversation_repo(redis_client):
    return ConversationRepository(redis_client)


@pytest.fixture
def rate_limiter_mock():
    """rate_limiter를 mock으로 대체 — 실제 Redis 연결 없음."""
    mock = MagicMock(spec=RateLimiter)
    mock.consume = AsyncMock(return_value=RateLimitResult(allowed=True, remaining=30, retry_after_ms=0))
    return mock


@pytest.fixture
def llm_mock():
    mock = AsyncMock()
    mock.chat = AsyncMock(return_value="LLM 응답입니다.")
    # 한 단어 약 1토큰으로 단순 카운트
    mock.count_tokens = MagicMock(side_effect=lambda text: max(1, len((text or "").split())))
    # 임베딩: 빈 리스트 반환 → retrieval 섹션 생략
    mock.embed = AsyncMock(return_value=[])
    return mock


@pytest.fixture
def session_mock():
    return MagicMock()


@pytest.fixture
def service_factory(monkeypatch, session_mock, llm_mock, conversation_repo, rate_limiter_mock):
    """ConversationService를 만들면서 내부 repository 들을 mock으로 치환."""

    def _make(*, document=None, job=None):
        document = document if document is not None else _build_document()
        job = job if job is not None else _build_completed_job()

        service = ConversationService(
            session_mock,
            llm_mock,
            conversation_repository=conversation_repo,
            rate_limiter=rate_limiter_mock,  # 실제 Redis 연결 방지
        )

        service._documents.get_by_id = AsyncMock(return_value=document)
        service._jobs.get_latest_by_document = AsyncMock(return_value=job)
        # retrieval repository — 빈 결과 반환으로 RAG 섹션 생략
        service._context = AsyncMock()
        service._context.find_my_blog_context = AsyncMock(return_value=[])
        service._context.find_job_posting_context = AsyncMock(return_value=[])
        return service

    return _make


class TestConversationService:
    async def test_chat_appends_history_and_tracks_tokens(self, service_factory, redis_client):
        service = service_factory()
        request = ChatRequest(
            user_id=1, session_id="s1", document_id=10, message="이 글의 톤은 어때?"
        )

        response = await service.chat(request)

        assert response.session_id == "s1"
        assert response.reply == "LLM 응답입니다."
        assert response.tokens_used > 0
        assert response.tokens_remaining >= 0

        # Redis에 user, assistant 메시지가 모두 저장됐는지 확인
        repo = service._conversations
        history = await repo.get_messages("s1")
        assert len(history) == 2
        assert history[0].role == ChatRole.USER
        assert history[1].role == ChatRole.ASSISTANT

    async def test_chat_blocks_when_document_belongs_to_other_user(self, service_factory):
        other_user_doc = _build_document(user_id=999)
        service = service_factory(document=other_user_doc)

        with pytest.raises(NotFoundError):
            await service.chat(
                ChatRequest(user_id=1, session_id="s1", document_id=10, message="hi")
            )

    async def test_chat_raises_when_token_limit_reached(
        self, service_factory, conversation_repo, monkeypatch
    ):
        service = service_factory()
        # 누적 사용량을 한도 직전까지 채워두기
        await conversation_repo.add_token_usage("s1", service._settings.max_conversation_tokens - 1)

        with pytest.raises(TokenLimitExceededError):
            await service.chat(
                ChatRequest(
                    user_id=1, session_id="s1", document_id=10, message="이 정도면 한도 초과"
                )
            )

    async def test_reset_session_clears_state(self, service_factory, conversation_repo):
        service = service_factory()
        await service.chat(
            ChatRequest(user_id=1, session_id="s2", document_id=10, message="안녕")
        )
        assert await conversation_repo.get_token_usage("s2") > 0

        await service.reset_session("s2")
        assert await conversation_repo.get_token_usage("s2") == 0
        assert await conversation_repo.get_messages("s2") == []
