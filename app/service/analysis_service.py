from __future__ import annotations

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
from app.repository.context_retrieval_repository import ContextRetrievalRepository
from app.repository.document_repository import DocumentRepository

logger = get_logger(__name__)

_POST_SYSTEM_PROMPT = """\
당신은 블로그 글 분석 및 인플루언서 마케팅 전문가입니다.
제공된 블로그 글(그리고 선택적으로 참고 블로그 글·공고 컨텍스트)을 분석하여 \
아래 JSON 스키마에 정확히 일치하는 응답을 반환하세요. 다른 텍스트는 포함하지 마세요.

{
  "summary": "글의 핵심 요약 (2-3문장)",
  "key_topics": ["주요 주제 키워드 (3-7개)"],
  "tone": "글의 톤/문체 (예: 분석적, 친근함, 전문적)",
  "target_audience": "예상 독자층",
  "suggestions": ["글 개선 제안사항 (2-4개)"],
  "overall_score": "이 포스트의 품질 점수 (0-100 정수)",
  "percentile": "유사 블로그 대비 상위 퍼센타일 추정 (0-100 정수)",
  "blog_type": "블로그 유형 (예: 라이프스타일, 맛집/여행, 뷰티, IT/기술, 육아, 패션)",
  "strength_summary": "이 포스트의 핵심 강점 한 문장",
  "weakness_summary": "개선이 필요한 부분 한 문장",
  "top_categories": [
    {"category": "카테고리명 (FOOD/BEAUTY/LIVING/FASHION/TECH/TRAVEL 중)", "score": "점수 0-100 정수"}
  ],
  "metrics": [
    {"name": "콘텐츠 품질", "score": "점수 0-100 정수"},
    {"name": "정보 충실도", "score": "점수 0-100 정수"},
    {"name": "사진 활용", "score": "점수 0-100 정수"},
    {"name": "독자 친화도", "score": "점수 0-100 정수"},
    {"name": "SEO 최적화", "score": "점수 0-100 정수"},
    {"name": "일관성", "score": "점수 0-100 정수"}
  ]
}
"""

_FULL_BLOG_SYSTEM_PROMPT = """\
당신은 블로그 채널 전체 분석 및 광고주 매칭 전문가입니다.
여러 포스트를 집약한 블로그 스냅샷을 분석하여 채널 전반의 특성과 협업 적합성을 평가하세요.
아래 JSON 스키마에 정확히 일치하는 응답을 반환하세요. 다른 텍스트는 포함하지 마세요.

{
  "summary": "블로그 채널 전체의 핵심 성격 요약 (2-3문장)",
  "key_topics": ["블로그 전반에 걸친 주요 주제 키워드 (5-10개)"],
  "tone": "글 전반의 톤/문체 (예: 분석적, 친근함, 감성적, 전문적)",
  "target_audience": "이 블로그 채널의 주요 독자층",
  "suggestions": ["채널 전략 개선 제안 (3-5개)"],
  "overall_score": "블로그 채널 전반의 품질 및 영향력 점수 (0-100 정수)",
  "percentile": "동종 블로거 채널 대비 상위 퍼센타일 추정 (0-100 정수)",
  "blog_type": "블로그 유형 (예: 라이프스타일, 맛집/여행, 뷰티, IT/기술, 육아, 패션)",
  "strength_summary": "이 블로그 채널의 핵심 강점 한 문장",
  "weakness_summary": "채널 차원에서 개선이 필요한 부분 한 문장",
  "top_categories": [
    {"category": "카테고리명 (FOOD/BEAUTY/LIVING/FASHION/TECH/TRAVEL 중)", "score": "점수 0-100 정수"}
  ],
  "metrics": [
    {"name": "콘텐츠 일관성", "score": "점수 0-100 정수"},
    {"name": "카테고리 전문성", "score": "점수 0-100 정수"},
    {"name": "독자 커버리지", "score": "점수 0-100 정수"},
    {"name": "광고 친화도", "score": "점수 0-100 정수"},
    {"name": "브랜드 협업 적합성", "score": "점수 0-100 정수"},
    {"name": "성장 가능성", "score": "점수 0-100 정수"}
  ],
  "benchmark_summary": "유사 인플루언서 블로그 대비 비교 요약 한 문장 (유사 인플루언서 컨텍스트가 없으면 null)",
  "competitive_strengths": ["유사 인플루언서 대비 강점 (최대 3개, 없으면 빈 배열)"],
  "competitive_gaps": ["유사 인플루언서 대비 보완점 (최대 3개, 없으면 빈 배열)"],
  "reference_blogger_patterns": ["참고할 만한 인플루언서 운영 패턴 (최대 3개, 없으면 빈 배열)"]
}
"""

_RAG_CONTEXT_TEMPLATE = """\
=== 참고: 동일 블로거의 이전 글 ===
{blog_context}

=== 참고: 유사 인플루언서 블로그 글 ===
{influencer_context}

=== 참고: 유사 공고 ===
{campaign_context}

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
        self._context = ContextRetrievalRepository(session)
        self._rate_limiter = rate_limiter or RateLimiter(get_redis())
        self._settings = get_settings()

    async def analyze(
        self, request: AnalysisRequest, *, enforce_rate_limit: bool = True
    ) -> AnalysisJob:
        if request.document_id is None:
            raise NotFoundError("document_id is required for analysis")
        if enforce_rate_limit:
            await self._enforce_rate_limit(request.user_id)
        document = await self._documents.get_by_id(request.document_id)
        if document is None:
            raise NotFoundError(f"document not found: {request.document_id}")

        analysis_mode = request.analysis_mode or "POST"
        job = await self._jobs.create(request.user_id, request.document_id)
        await self._jobs.update_status(job, AnalysisStatus.IN_PROGRESS)

        try:
            if self._settings.demo_mode or self._settings.llm_provider.lower() == "demo":
                result = self._demo_analysis_result(analysis_mode)
                logger.info("analysis completed (demo mode) job_id=%s mode=%s", job.id, analysis_mode)
            else:
                rag_context = await self._build_rag_context(document.id, request.user_id)
                result = await self._run_llm_analysis(
                    document.title, document.content, rag_context, analysis_mode
                )
                logger.info("analysis completed job_id=%s mode=%s", job.id, analysis_mode)

            result_dict = result.model_dump()
            result_dict["analysis_mode"] = analysis_mode
            if request.batch_id:
                result_dict["batch_id"] = request.batch_id
            if request.source_document_ids:
                result_dict["source_document_ids"] = request.source_document_ids

            await self._jobs.update_status(job, AnalysisStatus.COMPLETED, result=result_dict)
        except LLMClientError as exc:
            logger.exception("analysis failed job_id=%s", job.id)
            await self._jobs.update_status(job, AnalysisStatus.FAILED, error_message=str(exc))
            raise
        except Exception as exc:
            logger.exception("analysis failed job_id=%s", job.id)
            await self._jobs.update_status(job, AnalysisStatus.FAILED, error_message=str(exc))
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

    def _demo_analysis_result(self, analysis_mode: str = "POST") -> AnalysisResult:
        if analysis_mode == "FULL_BLOG":
            return AnalysisResult(
                summary="블로그 채널 전체 분석 결과입니다. (데모 모드 고정값)",
                key_topics=["라이프스타일", "맛집", "뷰티", "리뷰", "체험단", "일상"],
                tone="친근하고 감성적인 문체",
                target_audience="20-30대 여성 독자",
                suggestions=["카테고리 전문성 강화", "콘텐츠 발행 주기 유지", "브랜드 협업 포트폴리오 구성"],
                overall_score=81,
                percentile=75,
                blog_type="라이프스타일 블로거",
                strength_summary="다양한 카테고리를 친근한 문체로 꾸준히 발행하는 채널 신뢰성이 강점입니다.",
                weakness_summary="카테고리 집중도를 높이면 광고주 매칭 적합성이 향상됩니다.",
                top_categories=[
                    {"category": "FOOD", "score": 85},
                    {"category": "BEAUTY", "score": 72},
                    {"category": "LIVING", "score": 60},
                ],
                metrics=[
                    {"name": "콘텐츠 일관성", "score": 80},
                    {"name": "카테고리 전문성", "score": 65},
                    {"name": "독자 커버리지", "score": 78},
                    {"name": "광고 친화도", "score": 82},
                    {"name": "브랜드 협업 적합성", "score": 74},
                    {"name": "성장 가능성", "score": 70},
                ],
                analysis_mode="FULL_BLOG",
            )
        return AnalysisResult(
            summary="블로그 포스트 분석 결과입니다. (데모 모드 고정값)",
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
            analysis_mode="POST",
        )

    async def _build_rag_context(self, document_id: int, user_id: int) -> str | None:
        """분석 대상 문서의 대표 임베딩으로 관련 블로그·인플루언서·공고 청크를 검색해 컨텍스트 문자열을 만든다."""
        try:
            avg_emb = await self._context.get_document_avg_embedding(document_id)
            if avg_emb is None:
                return None

            blog_chunks = await self._context.find_my_blog_context(
                user_id=user_id,
                embedding=avg_emb,
                exclude_document_id=document_id,
                top_k=3,
            )
            ext_blog_chunks = await self._context.find_ext_blog_context(
                embedding=avg_emb,
                top_k=3,
            )
            job_chunks = await self._context.find_job_posting_context(
                embedding=avg_emb,
                top_k=3,
            )

            if not blog_chunks and not ext_blog_chunks and not job_chunks:
                return None

            blog_text = "\n\n".join(
                f"[{c.title}]\n{c.content_preview}" for c in blog_chunks
            ) or "관련 블로그 글 없음"
            influencer_text = "\n\n".join(
                f"[{c.title}]\n{c.content_preview}" for c in ext_blog_chunks
            ) or "관련 인플루언서 글 없음"
            campaign_text = "\n\n".join(
                f"[{c.title}]\n{c.content_preview}" for c in job_chunks
            ) or "관련 공고 없음"

            logger.info(
                "rag context built document_id=%s blog_chunks=%s ext_blog_chunks=%s job_chunks=%s",
                document_id, len(blog_chunks), len(ext_blog_chunks), len(job_chunks),
            )
            return _RAG_CONTEXT_TEMPLATE.format(
                blog_context=blog_text,
                influencer_context=influencer_text,
                campaign_context=campaign_text,
            )
        except Exception as exc:
            logger.warning("rag context build failed document_id=%s err=%s", document_id, exc)
            return None

    async def _run_llm_analysis(
        self,
        title: str,
        content: str,
        rag_context: str | None = None,
        analysis_mode: str = "POST",
    ) -> AnalysisResult:
        system_prompt = (
            _FULL_BLOG_SYSTEM_PROMPT if analysis_mode == "FULL_BLOG" else _POST_SYSTEM_PROMPT
        )
        # FULL_BLOG 스냅샷은 이미 포스트 내용을 잘라서 집계했으므로 20000자까지 허용
        max_chars = 20000 if analysis_mode == "FULL_BLOG" else 10000
        truncated = content[:max_chars]
        user_content = f"제목: {title}\n\n본문:\n{truncated}"
        if rag_context:
            user_content = rag_context + user_content
        # 컨텍스트가 여러 출처(블로그/공고/인플루언서)를 섞어 넣다 보니 일부 로컬 모델이
        # 지시를 무시하고 일반 대화 답변을 하는 경우가 있어, 마지막에 지시를 한 번 더 강조한다.
        user_content += (
            "\n\n---\n위 내용을 분석 대상으로 삼아, 다른 설명이나 대화 없이 "
            "system 메시지의 JSON 스키마와 정확히 일치하는 JSON 객체만 출력하세요."
        )

        raw = await self._llm.chat(
            messages=[
                ChatMessage(role=ChatRole.SYSTEM, content=system_prompt),
                ChatMessage(role=ChatRole.USER, content=user_content),
            ],
            temperature=0.3,
            # thinking 모델(Ollama qwen3/gemma 등)이 reasoning에 토큰을 소비하면
            # 실제 JSON 응답이 빈 문자열로 잘리므로 여유 있게 잡는다.
            max_tokens=6000,
            # Ollama는 schema를 얹어주면 문법 수준에서 정확히 이 필드로만 응답하도록 강제할 수 있다
            # (프롬프트 지시만으로는 참고 컨텍스트의 포맷을 그대로 따라 하는 경우가 있었다).
            response_format={"type": "json_object", "schema": AnalysisResult.model_json_schema()},
        )
        try:
            parsed = json.loads(raw)
            return AnalysisResult.model_validate(parsed)
        except (json.JSONDecodeError, ValueError) as exc:
            raise LLMClientError(f"invalid LLM JSON response: {exc}") from exc
