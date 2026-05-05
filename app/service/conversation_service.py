from sqlalchemy.ext.asyncio import AsyncSession

from app.client.llm_client import LLMClient
from app.client.redis_client import get_redis
from app.core.config import get_settings
from app.core.exceptions import (
    NotFoundError,
    RateLimitExceededError,
    TokenLimitExceededError,
)
from app.core.logging import get_logger
from app.core.rate_limiter import RateLimiter
from app.domain.enums import AnalysisStatus, ChatRole
from app.domain.schemas import ChatMessage, ChatRequest, ChatResponse
from app.repository.analysis_repository import AnalysisJobRepository
from app.repository.conversation_repository import ConversationRepository
from app.repository.document_repository import DocumentRepository

logger = get_logger(__name__)

_CHAT_SYSTEM_PROMPT_TEMPLATE = """\
당신은 사용자의 블로그 글을 분석한 결과를 바탕으로 대화하는 어시스턴트입니다.
사용자가 글에 대해 질문하거나 개선 방향을 논의할 수 있도록 도와주세요.

[분석 대상 글 제목]
{title}

[글 분석 결과]
{analysis}

위 정보를 기반으로 사용자의 질문에 친절하고 구체적으로 답변하세요.
"""


class ConversationService:
    """분석 결과 기반 대화형 기능. 토큰 한도 + 세션 TTL 적용."""

    def __init__(
        self,
        session: AsyncSession,
        llm_client: LLMClient,
        conversation_repository: ConversationRepository | None = None,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self._session = session
        self._llm = llm_client
        self._documents = DocumentRepository(session)
        self._jobs = AnalysisJobRepository(session)
        self._conversations = conversation_repository or ConversationRepository(
            get_redis()
        )
        self._rate_limiter = rate_limiter or RateLimiter(get_redis())
        self._settings = get_settings()

    async def chat(self, request: ChatRequest) -> ChatResponse:
        await self._enforce_rate_limit(request.user_id)
        await self._verify_context(request)

        used_tokens = await self._conversations.get_token_usage(request.session_id)
        request_tokens = self._llm.count_tokens(request.message)

        if used_tokens + request_tokens >= self._settings.max_conversation_tokens:
            raise TokenLimitExceededError(
                f"session token limit reached "
                f"({used_tokens}/{self._settings.max_conversation_tokens})"
            )

        history = await self._conversations.get_messages(request.session_id)
        if len(history) >= self._settings.max_turns_per_session * 2:
            raise TokenLimitExceededError("max turns per session reached")

        # System prompt는 매번 분석 결과로 새로 생성 (Redis에 저장 X, 토큰 절약)
        system_prompt = await self._build_system_prompt(request.document_id)

        user_message = ChatMessage(role=ChatRole.USER, content=request.message)
        messages_for_llm = [
            ChatMessage(role=ChatRole.SYSTEM, content=system_prompt),
            *history,
            user_message,
        ]

        reply_text = await self._llm.chat(
            messages=messages_for_llm,
            temperature=0.7,
            max_tokens=800,
        )
        reply_message = ChatMessage(role=ChatRole.ASSISTANT, content=reply_text)

        # 사용자/응답 메시지 모두 히스토리에 추가
        await self._conversations.append_message(request.session_id, user_message)
        await self._conversations.append_message(request.session_id, reply_message)

        consumed = request_tokens + self._llm.count_tokens(reply_text)
        new_total = await self._conversations.add_token_usage(
            request.session_id, consumed
        )

        return ChatResponse(
            session_id=request.session_id,
            reply=reply_text,
            tokens_used=new_total,
            tokens_remaining=max(
                0, self._settings.max_conversation_tokens - new_total
            ),
        )

    async def reset_session(self, session_id: str) -> None:
        await self._conversations.clear(session_id)

    async def _enforce_rate_limit(self, user_id: int) -> None:
        result = await self._rate_limiter.consume(
            scope="chat",
            user_id=user_id,
            capacity=self._settings.chat_rate_capacity,
            refill_per_sec=self._settings.chat_rate_refill_per_sec,
        )
        if not result.allowed:
            raise RateLimitExceededError(
                "chat rate limit exceeded",
                retry_after_ms=result.retry_after_ms,
            )

    async def _verify_context(self, request: ChatRequest) -> None:
        document = await self._documents.get_by_id(request.document_id)
        if document is None or document.user_id != request.user_id:
            raise NotFoundError(f"document not accessible: {request.document_id}")

    async def _build_system_prompt(self, document_id: int) -> str:
        document = await self._documents.get_by_id(document_id)
        if document is None:
            raise NotFoundError(f"document not found: {document_id}")

        job = await self._jobs.get_latest_by_document(document_id)
        if job is None or job.status != AnalysisStatus.COMPLETED:
            analysis_text = "분석이 아직 완료되지 않았습니다."
        else:
            analysis_text = self._format_analysis(job.result)

        return _CHAT_SYSTEM_PROMPT_TEMPLATE.format(
            title=document.title, analysis=analysis_text
        )

    @staticmethod
    def _format_analysis(result: dict) -> str:
        lines = [
            f"- 요약: {result.get('summary', '')}",
            f"- 주요 토픽: {', '.join(result.get('key_topics', []))}",
            f"- 톤: {result.get('tone', '')}",
            f"- 타겟 독자: {result.get('target_audience', '')}",
            f"- 개선 제안: {'; '.join(result.get('suggestions', []))}",
        ]
        return "\n".join(lines)
