from __future__ import annotations

import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.client.llm_client import LLMClient
from app.core.config import get_settings
from app.core.exceptions import LLMClientError, NotFoundError
from app.core.logging import get_logger
from app.domain.enums import ChatRole
from app.domain.models import BlogDiagnosis
from app.domain.schemas import ChatMessage
from app.repository.analysis_repository import AnalysisJobRepository
from app.repository.context_retrieval_repository import ContextRetrievalRepository
from app.repository.document_repository import DocumentRepository

logger = get_logger(__name__)

_DIAGNOSIS_SYSTEM_PROMPT = """\
당신은 블로그 채널 전문 진단 분석가입니다.
제공된 블로그 채널 분석 결과와 메타데이터를 바탕으로 아래 JSON 스키마에 정확히 일치하는 6지표 진단 결과를 반환하세요.
다른 텍스트는 포함하지 마세요.

{
  "metrics": {
    "topic_consistency": {"score": 0~100 정수, "comment": "한 줄 설명"},
    "image_usage":       {"score": 0~100 정수, "comment": "한 줄 설명", "confidence": "HIGH|MEDIUM|LOW"},
    "view_per_post":     {"score": 0~100 정수, "comment": "한 줄 설명", "confidence": "HIGH|MEDIUM|LOW"},
    "keyword_usage":     {"score": 0~100 정수, "comment": "한 줄 설명"},
    "interaction":       {"score": 0~100 정수, "comment": "한 줄 설명"},
    "informativeness":   {"score": 0~100 정수, "comment": "한 줄 설명"}
  },
  "category_fit": [
    {"category": "FOOD|BEAUTY|LIVING|FASHION|TECH|TRAVEL 중 하나", "score": 0~100 정수}
  ],
  "strengths": ["강점 카드 (2~3개)"],
  "weaknesses": ["약점 카드 (2~3개)"]
}

주의: view_per_post와 image_usage는 실제 데이터가 없으면 confidence=LOW로 표시하고 점수는 보수적으로 산출하세요.
"""


class BlogDiagnosisService:
    """R2: 6지표 진단. 집계형 지표는 DB에서, 판단형은 LLM에서 처리한다."""

    def __init__(self, session: AsyncSession, llm_client: LLMClient) -> None:
        self._session = session
        self._llm = llm_client
        self._jobs = AnalysisJobRepository(session)
        self._documents = DocumentRepository(session)
        self._context = ContextRetrievalRepository(session)
        self._settings = get_settings()

    async def diagnose(self, user_id: int, document_id: int) -> BlogDiagnosis:
        document = await self._documents.get_by_id(document_id)
        if document is None or document.user_id != user_id:
            raise NotFoundError(f"document not found or not owned: {document_id}")

        job = await self._jobs.get_latest_by_document(document_id)
        analysis_result = job.result if job else {}

        meta = document.doc_metadata or {}
        aggregated = self._aggregate_metrics(meta, analysis_result)

        if self._settings.demo_mode:
            diagnosis_raw = self._demo_diagnosis(aggregated)
        else:
            diagnosis_raw = await self._run_llm_diagnosis(document, analysis_result, aggregated)

        embedding = await self._build_result_embedding(diagnosis_raw, document_id)

        diag = BlogDiagnosis(
            user_id=user_id,
            analysis_job_id=job.id if job else None,
            metrics=diagnosis_raw.get("metrics", {}),
            category_fit=diagnosis_raw.get("category_fit", []),
            strengths=diagnosis_raw.get("strengths", []),
            weaknesses=diagnosis_raw.get("weaknesses", []),
            result_embedding=embedding,
        )
        self._session.add(diag)
        await self._session.flush()
        await self._session.refresh(diag)
        return diag

    def _aggregate_metrics(self, meta: dict, analysis: dict) -> dict:
        """결정론적 집계형 지표를 doc_metadata에서 산출한다."""
        like_count = meta.get("like_count")
        comment_count = meta.get("comment_count")
        image_count = meta.get("image_count")
        view_count = meta.get("view_count")

        interaction_score = None
        if like_count is not None and comment_count is not None:
            raw = (like_count + comment_count)
            interaction_score = min(100, int(raw / max(1, meta.get("post_count", 1)) * 10))

        image_score = None
        image_confidence = "LOW"
        if image_count is not None:
            image_score = min(100, image_count * 15)
            image_confidence = "HIGH"
        elif meta.get("thumbnail_image_url"):
            image_score = 30
            image_confidence = "MEDIUM"

        view_score = None
        view_confidence = "LOW"
        if view_count is not None:
            view_score = min(100, int(view_count / max(1, meta.get("post_count", 1)) / 10))
            view_confidence = "HIGH"

        return {
            "interaction_score": interaction_score,
            "image_score": image_score,
            "image_confidence": image_confidence,
            "view_score": view_score,
            "view_confidence": view_confidence,
            "existing_metrics": analysis.get("metrics", []),
            "existing_top_categories": analysis.get("top_categories", []),
        }

    async def _run_llm_diagnosis(self, document, analysis: dict, aggregated: dict) -> dict:
        user_content = (
            f"[블로그 분석 결과]\n{json.dumps(analysis, ensure_ascii=False)}\n\n"
            f"[집계 지표 사전 계산값]\n{json.dumps(aggregated, ensure_ascii=False)}\n\n"
            f"[본문 일부]\n{document.content[:3000]}"
        )
        raw = await self._llm.chat(
            messages=[
                ChatMessage(role=ChatRole.SYSTEM, content=_DIAGNOSIS_SYSTEM_PROMPT),
                ChatMessage(role=ChatRole.USER, content=user_content),
            ],
            temperature=0.2,
            max_tokens=1200,
            response_format={"type": "json_object"},
        )
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMClientError(f"invalid LLM JSON: {exc}") from exc

    def _demo_diagnosis(self, aggregated: dict) -> dict:
        return {
            "metrics": {
                "topic_consistency": {"score": 78, "comment": "주제가 비교적 일관됩니다."},
                "image_usage": {"score": aggregated.get("image_score") or 50,
                                "comment": "이미지 활용 양호",
                                "confidence": aggregated.get("image_confidence", "LOW")},
                "view_per_post": {"score": aggregated.get("view_score") or 0,
                                  "comment": "조회수 데이터 미수집",
                                  "confidence": aggregated.get("view_confidence", "LOW")},
                "keyword_usage": {"score": 65, "comment": "핵심 키워드 반복 활용이 필요합니다."},
                "interaction": {"score": aggregated.get("interaction_score") or 55,
                                "comment": "좋아요·댓글 평균 수준"},
                "informativeness": {"score": 72, "comment": "정보 밀도가 양호합니다."},
            },
            "category_fit": [
                {"category": "FOOD", "score": 80},
                {"category": "BEAUTY", "score": 60},
            ],
            "strengths": ["꾸준한 발행 주기", "감성적 사진 활용"],
            "weaknesses": ["카테고리 집중도 보완 필요", "SEO 키워드 강화"],
        }

    async def _build_result_embedding(self, diagnosis: dict, document_id: int) -> list[float] | None:
        """진단 요약 텍스트를 임베딩하여 R4 추천 쿼리 벡터로 사용한다."""
        try:
            strengths = " ".join(diagnosis.get("strengths", []))
            weaknesses = " ".join(diagnosis.get("weaknesses", []))
            text = f"강점: {strengths} 약점: {weaknesses}"
            results = await self._llm.embed([text])
            return results[0] if results else None
        except Exception as exc:
            logger.warning("diagnosis embedding failed document_id=%s err=%s", document_id, exc)
            return None
