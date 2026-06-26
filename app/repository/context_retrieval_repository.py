from __future__ import annotations

"""RAG 분석 context 조회.

분석 서비스에서 LLM 프롬프트에 포함할 관련 청크를 pgvector로 검색한다.
"""

from dataclasses import dataclass

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class ContextChunk:
    document_id: int
    title: str
    content_preview: str
    source_type: str
    score: float


class ContextRetrievalRepository:
    """분석 대상 문서 임베딩을 기준으로 관련 청크를 검색한다."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_document_avg_embedding(self, document_id: int) -> list[float] | None:
        """문서의 청크 임베딩 평균(대표 벡터)을 반환."""
        sql = text(
            """
            SELECT avg(embedding) AS avg_emb
            FROM document_chunks
            WHERE document_id = :document_id
            """
        )
        result = await self._session.execute(sql, {"document_id": document_id})
        row = result.mappings().one_or_none()
        if row is None or row["avg_emb"] is None:
            return None
        emb = row["avg_emb"]
        if isinstance(emb, list):
            return emb
        # pgvector returns vector as "[0.1,0.2,...]" string via asyncpg
        if isinstance(emb, str):
            return [float(x) for x in emb.strip("[]").split(",")]
        return list(emb)

    async def find_my_blog_context(
        self,
        *,
        user_id: int,
        embedding: list[float],
        exclude_document_id: int,
        top_k: int = 5,
    ) -> list[ContextChunk]:
        """사용자 자신의 블로그 청크 중 분석 대상과 유사한 것을 검색.

        CTE로 문서별 최고 유사 청크를 먼저 선택한 뒤 score DESC 재정렬로
        전역 유사도 기준 top_k를 보장한다.
        """
        sql = text(
            """
            -- DISTINCT ON은 ORDER BY가 d.id로 시작해야 하므로, 여기서 LIMIT까지 걸면
            -- 낮은 document_id가 유사도 높은 문서보다 먼저 잘릴 수 있다.
            -- CTE에서는 문서별 최고 유사 청크만 고르고, 바깥 쿼리에서 score DESC로
            -- 다시 정렬한 뒤 LIMIT을 적용해 전역 유사도 top_k를 보장한다.
            WITH best_chunk_per_doc AS (
                SELECT DISTINCT ON (d.id)
                    d.id AS document_id,
                    d.title,
                    LEFT(c.content, 300) AS content_preview,
                    d.source_type,
                    1 - (c.embedding <=> CAST(:embedding AS vector)) AS score
                FROM document_chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE d.user_id = :user_id
                  AND d.source_type = 'my_blog'
                  AND d.id != :exclude_doc
                ORDER BY d.id, c.embedding <=> CAST(:embedding AS vector)
            )
            SELECT * FROM best_chunk_per_doc
            ORDER BY score DESC
            LIMIT :top_k
            """
        ).bindparams(
            bindparam("embedding"),
            bindparam("user_id"),
            bindparam("exclude_doc"),
            bindparam("top_k"),
        )
        result = await self._session.execute(
            sql,
            {
                "embedding": str(embedding),
                "user_id": user_id,
                "exclude_doc": exclude_document_id,
                "top_k": top_k,
            },
        )
        return [
            ContextChunk(
                document_id=row["document_id"],
                title=row["title"],
                content_preview=row["content_preview"],
                source_type=row["source_type"],
                score=float(row["score"]),
            )
            for row in result.mappings().all()
        ]

    async def find_ext_blog_context(
        self,
        *,
        embedding: list[float],
        top_k: int = 3,
    ) -> list[ContextChunk]:
        """분석 대상 블로그와 유사한 인플루언서 블로그 청크를 검색.

        CTE로 문서별 최고 유사 청크를 먼저 선택한 뒤 score DESC 재정렬로
        전역 유사도 기준 top_k를 보장한다.
        """
        sql = text(
            """
            -- DISTINCT ON은 ORDER BY가 d.id로 시작해야 하므로, 여기서 LIMIT까지 걸면
            -- 낮은 document_id가 유사도 높은 문서보다 먼저 잘릴 수 있다.
            -- CTE에서는 문서별 최고 유사 청크만 고르고, 바깥 쿼리에서 score DESC로
            -- 다시 정렬한 뒤 LIMIT을 적용해 전역 유사도 top_k를 보장한다.
            WITH best_chunk_per_doc AS (
                SELECT DISTINCT ON (d.id)
                    d.id AS document_id,
                    d.title,
                    LEFT(c.content, 300) AS content_preview,
                    d.source_type,
                    d.doc_metadata,
                    1 - (c.embedding <=> CAST(:embedding AS vector)) AS score
                FROM document_chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE d.source_type = 'ext_blog'
                ORDER BY d.id, c.embedding <=> CAST(:embedding AS vector)
            )
            SELECT * FROM best_chunk_per_doc
            ORDER BY score DESC
            LIMIT :top_k
            """
        ).bindparams(
            bindparam("embedding"),
            bindparam("top_k"),
        )
        result = await self._session.execute(
            sql,
            {
                "embedding": str(embedding),
                "top_k": top_k,
            },
        )
        return [
            ContextChunk(
                document_id=row["document_id"],
                title=row["title"],
                content_preview=row["content_preview"],
                source_type=row["source_type"],
                score=float(row["score"]),
            )
            for row in result.mappings().all()
        ]

    async def find_job_posting_context(
        self,
        *,
        embedding: list[float],
        top_k: int = 5,
    ) -> list[ContextChunk]:
        """분석 대상 블로그와 유사한 공고 청크를 검색.

        CTE로 문서별 최고 유사 청크를 먼저 선택한 뒤 score DESC 재정렬로
        전역 유사도 기준 top_k를 보장한다.
        """
        sql = text(
            """
            -- DISTINCT ON은 ORDER BY가 d.id로 시작해야 하므로, 여기서 LIMIT까지 걸면
            -- 낮은 document_id가 유사도 높은 문서보다 먼저 잘릴 수 있다.
            -- CTE에서는 문서별 최고 유사 청크만 고르고, 바깥 쿼리에서 score DESC로
            -- 다시 정렬한 뒤 LIMIT을 적용해 전역 유사도 top_k를 보장한다.
            WITH best_chunk_per_doc AS (
                SELECT DISTINCT ON (d.id)
                    d.id AS document_id,
                    d.title,
                    LEFT(c.content, 300) AS content_preview,
                    d.source_type,
                    1 - (c.embedding <=> CAST(:embedding AS vector)) AS score
                FROM document_chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE d.source_type = 'job_posting'
                ORDER BY d.id, c.embedding <=> CAST(:embedding AS vector)
            )
            SELECT * FROM best_chunk_per_doc
            ORDER BY score DESC
            LIMIT :top_k
            """
        ).bindparams(
            bindparam("embedding"),
            bindparam("top_k"),
        )
        result = await self._session.execute(
            sql,
            {
                "embedding": str(embedding),
                "top_k": top_k,
            },
        )
        return [
            ContextChunk(
                document_id=row["document_id"],
                title=row["title"],
                content_preview=row["content_preview"],
                source_type=row["source_type"],
                score=float(row["score"]),
            )
            for row in result.mappings().all()
        ]
