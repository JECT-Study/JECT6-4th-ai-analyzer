from datetime import datetime
from typing import Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.domain.enums import AnalysisStatus, SourceType

EMBEDDING_DIM = 768  # nomic-embed-text


class Base(DeclarativeBase):
    pass


class Document(Base):
    """크롤링/업로드된 원문 글 (블로그, 공고 등)."""

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    source_type: Mapped[SourceType] = mapped_column(String(32), nullable=False)
    external_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    url: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    doc_metadata: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    content_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    crawled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ingestion_status: Mapped[str] = mapped_column(
        String(32), default="completed", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_documents_user_source", "user_id", "source_type"),
        Index("ix_documents_external_id", "external_id"),
        UniqueConstraint(
            "user_id", "source_type", "external_id",
            name="uq_documents_user_source_external",
        ),
    )


class DocumentChunk(Base):
    """청크 + 임베딩."""

    __tablename__ = "document_chunks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    document: Mapped[Document] = relationship(back_populates="chunks")

    __table_args__ = (
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_chunks_document_id", "document_id"),
    )


class BlogDiagnosis(Base):
    """R2: 6지표 진단 결과. Analyzer가 소유하고, Spring은 read 위주로 접근한다."""

    __tablename__ = "blog_diagnoses"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    analysis_job_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("analysis_jobs.id", ondelete="SET NULL"), nullable=True
    )
    metrics: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    category_fit: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    strengths: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    weaknesses: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    result_embedding: Mapped[Optional[list[float]]] = mapped_column(
        Vector(EMBEDDING_DIM), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ProfileEmbedding(Base):
    """R4: 온보딩 프로필 임베딩. Spring이 read-only로 접근하여 벡터 추천에 사용한다."""

    __tablename__ = "profile_embeddings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
    profile_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # 최신 row 조회 쿼리(ORDER BY created_at DESC LIMIT 1) 최적화
        Index("ix_profile_embeddings_user_created", "user_id", "created_at"),
        # 동일 프로필 해시 중복 방지: NULL은 유니크 제약 대상 외
        UniqueConstraint("user_id", "profile_hash", name="uq_profile_embeddings_user_hash"),
    )


class Influencer(Base):
    """인플루언서 프로필. Spring이 primary owner, Analyzer가 ext_blog 수집 시 upsert."""

    __tablename__ = "influencer"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    influencer_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    blog_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    thumbnail_url: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    blog_url: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    category: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        UniqueConstraint("blog_url", name="uq_influencer_blog_url"),
    )


class AnalysisJob(Base):
    """블로그 분석 잡 상태 추적."""

    __tablename__ = "analysis_jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    document_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[AnalysisStatus] = mapped_column(
        String(32), default=AnalysisStatus.PENDING, nullable=False
    )
    result: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
