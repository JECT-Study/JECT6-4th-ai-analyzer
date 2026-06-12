from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import ChatRole, SourceType


# ===== Chunking =====
class ChunkRequest(BaseModel):
    user_id: int
    source_type: SourceType
    title: str
    content: str = Field(..., min_length=1)
    url: str | None = None
    external_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    content_hash: str | None = Field(default=None, min_length=64, max_length=64)
    crawled_at: datetime | None = None
    ingestion_status: str = Field(default="completed", max_length=32)


class ChunkResponse(BaseModel):
    document_id: int
    chunk_count: int


class CrawlJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: int
    url: str = Field(..., min_length=1, max_length=2048)
    source_type: SourceType = SourceType.EXT_BLOG
    title: str | None = Field(default=None, max_length=512)
    external_id: str | None = Field(default=None, max_length=255)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CrawlJobResponse(BaseModel):
    job_id: str
    stream: str


# ===== Similarity =====
# NOTE(2026-05-13): 프론트용 유사도 검색은 Spring 메인 서버 책임으로 이동했다.
# 이전 DTO는 이력 보존을 위해 주석으로 남긴다.
#
# class SimilarityMatchRequest(BaseModel):
#     user_id: int
#     query_text: str = Field(..., min_length=1)
#     target_source_type: SourceType = SourceType.MY_BLOG
#     top_k: int = Field(default=5, ge=1, le=50)
#     # HyDE: 쿼리가 공고/외부글일 때 LLM으로 가상 답변을 생성해 임베딩 매칭 품질을 높임
#     query_source_type: SourceType | None = None
#     use_hyde: bool = False
#     # Hybrid: 벡터 + BM25 결합 검색 (RRF). 키워드 매칭이 중요한 공고에 효과적
#     use_hybrid: bool = False
#     keywords: str | None = None  # 미지정 시 query_text를 그대로 키워드로 사용
#
#
# class SimilarDocument(BaseModel):
#     document_id: int
#     title: str
#     url: str | None
#     score: float
#     matched_chunk_preview: str
#
#
# class SimilarityMatchResponse(BaseModel):
#     matches: list[SimilarDocument]
#     rewritten_query: str | None = None  # HyDE 사용 시 변환된 쿼리


# ===== Analysis =====
class AnalysisRequest(BaseModel):
    """Queue 메시지에서도 동일하게 사용."""

    user_id: int
    document_id: int


class AnalysisResult(BaseModel):
    # 기존 5필드 유지
    summary: str
    key_topics: list[str]
    tone: str
    target_audience: str
    suggestions: list[str]
    # AI 대시보드 확장 필드
    overall_score: int | None = None
    percentile: int | None = None
    blog_type: str | None = None
    strength_summary: str | None = None
    weakness_summary: str | None = None
    top_categories: list[dict[str, Any]] | None = None
    metrics: list[dict[str, Any]] | None = None


class AnalysisJobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    document_id: int
    status: str
    result: dict[str, Any]
    error_message: str | None
    created_at: datetime
    updated_at: datetime


# ===== Conversation =====
class ChatMessage(BaseModel):
    role: ChatRole
    content: str


class ChatRequest(BaseModel):
    user_id: int
    session_id: str
    document_id: int  # 대화의 컨텍스트가 되는 분석 대상 글
    message: str = Field(..., min_length=1, max_length=4000)


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    tokens_used: int
    tokens_remaining: int
