import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.client.llm_client import LLMClient
from app.client.redis_client import get_redis
from app.core.config import get_settings
from app.core.exceptions import (
    LLMClientError,
    NotFoundError,
    RateLimitExceededError,
)
from app.core.logging import get_logger
from app.core.rate_limiter import RateLimiter
from app.domain.enums import AnalysisStatus, ChatRole
from app.domain.models import AnalysisJob
from app.domain.schemas import (
    AnalysisRequest,
    AnalysisResult,
    ChatMessage,
)
from app.repository.analysis_repository import AnalysisJobRepository
from app.repository.document_repository import DocumentRepository

logger = get_logger(__name__)

_ANALYSIS_SYSTEM_PROMPT = """\
당신은 블로그 글 분석 전문가입니다. 제공된 블로그 글을 분석하여 아래 JSON 스키마에 \
정확히 일치하는 응답을 반환하세요. 다른 텍스트는 포함하지 마세요.

{
  "summary": "글의 핵심 요약 (2-3문장)",
  "key_topics": ["주요 주제 키워드 (3-7개)"],
  "tone": "글의 톤/문체 (예: 분석적, 친근함, 전문적)",
  "target_audience": "예상 독자층",
  "suggestions": ["글 개선 제안사항 (2-4개)"]
}
"""


class AnalysisService:
    """블로그 글 분석 서비스. 큐 워커와 API 양쪽에서 호출 가능."""

    def __init__(
        self,
        session: AsyncSession,
        llm_client: LLMClient,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self._session = session
        self._llm = llm_client
        self._jobs = AnalysisJobRepository(session)
        self._documents = DocumentRepository(session)
        self._rate_limiter = rate_limiter or RateLimiter(get_redis())
        self._settings = get_settings()

    async def analyze(self, request: AnalysisRequest) -> AnalysisJob:
        await self._enforce_rate_limit(request.user_id)
        document = await self._documents.get_by_id(request.document_id)
        if document is None:
            raise NotFoundError(f"document not found: {request.document_id}")

        job = await self._jobs.create(request.user_id, request.document_id)
        await self._jobs.update_status(job, AnalysisStatus.IN_PROGRESS)

        try:
            if self._settings.demo_mode or self._settings.llm_provider.lower() == "demo":
                result = self._demo_analysis_result()
                logger.info("analysis completed (demo mode) job_id=%s", job.id)
            else:
                result = await self._run_llm_analysis(document.title, document.content)
                logger.info("analysis completed job_id=%s", job.id)
            await self._jobs.update_status(
                job, AnalysisStatus.COMPLETED, result=result.model_dump()
            )
        except LLMClientError as exc:
            logger.exception("analysis failed job_id=%s", job.id)
            await self._jobs.update_status(
                job, AnalysisStatus.FAILED, error_message=str(exc)
            )
            raise
        except Exception as exc:
            logger.exception("analysis failed job_id=%s", job.id)
            await self._jobs.update_status(
                job, AnalysisStatus.FAILED, error_message=str(exc)
            )
            raise

        await self._session.refresh(job)
        return job

    async def get_analysis_for_document(self, document_id: int) -> AnalysisJob:
        job = await self._jobs.get_latest_by_document(document_id)
        if job is None:
            raise NotFoundError(f"no analysis for document: {document_id}")
        return job

    async def _enforce_rate_limit(self, user_id: int) -> None:
        result = await self._rate_limiter.consume(
            scope="analysis",
            user_id=user_id,
            capacity=self._settings.analysis_rate_capacity,
            refill_per_sec=self._settings.analysis_rate_refill_per_sec,
        )
        if not result.allowed:
            raise RateLimitExceededError(
                "analysis rate limit exceeded",
                retry_after_ms=result.retry_after_ms,
            )

    def _demo_analysis_result(self) -> AnalysisResult:
        return AnalysisResult(
            summary="블로그 분석 결과입니다. (데모 모드 고정값)",
            key_topics=["블로그", "리뷰", "체험단", "맛집", "뷰티"],
            tone="친근하고 감성적인 문체",
            target_audience="20-30대 여성 독자",
            suggestions=["사진 품질 향상", "SEO 키워드 추가"],
            overall_score=78,
            percentile=72,
            blog_type="라이프스타일 블로거",
            strength_summary="감성적인 사진과 솔직한 후기가 강점입니다.",
            weakness_summary="정보성 콘텐츠 보완 시 검색 유입이 증가합니다.",
            top_categories=[
                {"category": "FOOD", "score": 85},
                {"category": "BEAUTY", "score": 72},
                {"category": "LIVING", "score": 60},
            ],
            metrics=[
                {"name": "콘텐츠 품질", "score": 80},
                {"name": "정보 충실도", "score": 65},
                {"name": "사진 활용", "score": 88},
                {"name": "독자 친화도", "score": 76},
                {"name": "SEO 최적화", "score": 70},
                {"name": "일관성", "score": 82},
            ],
        )

    async def _run_llm_analysis(self, title: str, content: str) -> AnalysisResult:
        # 너무 긴 본문은 잘라서 비용 통제 (필요시 map-reduce 확장)
        truncated = content[:12000]
        user_content = f"제목: {title}\n\n본문:\n{truncated}"

        raw = await self._llm.chat(
            messages=[
                ChatMessage(role=ChatRole.SYSTEM, content=_ANALYSIS_SYSTEM_PROMPT),
                ChatMessage(role=ChatRole.USER, content=user_content),
            ],
            temperature=0.3,
            max_tokens=1024,
            response_format={"type": "json_object"},
        )
        try:
            parsed = json.loads(raw)
            return AnalysisResult.model_validate(parsed)
        except (json.JSONDecodeError, ValueError) as exc:
            raise LLMClientError(f"invalid LLM JSON response: {exc}") from exc
