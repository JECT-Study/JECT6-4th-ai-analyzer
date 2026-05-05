from dataclasses import dataclass

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import SourceType


@dataclass(frozen=True)
class SimilarityHit:
    document_id: int
    title: str
    url: str | None
    score: float
    chunk_preview: str


class SimilarityRepository:
    """pgvector 기반 유사도 검색.

    청크 단위로 검색하되 document 단위로 집계하여 max score 반환.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def search_similar_documents(
        self,
        *,
        user_id: int,
        embedding: list[float],
        target_source_type: SourceType,
        top_k: int,
    ) -> list[SimilarityHit]:
        # cosine distance: 0(동일) ~ 2. similarity = 1 - distance
        # DISTINCT ON으로 document별 최고 점수 청크만 추림
        sql = text(
            """
            SELECT document_id, title, url, score, chunk_preview
            FROM (
                SELECT DISTINCT ON (d.id)
                    d.id AS document_id,
                    d.title,
                    d.url,
                    1 - (c.embedding <=> CAST(:embedding AS vector)) AS score,
                    LEFT(c.content, 200) AS chunk_preview
                FROM document_chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE d.user_id = :user_id
                  AND d.source_type = :source_type
                ORDER BY d.id, c.embedding <=> CAST(:embedding AS vector)
            ) t
            ORDER BY score DESC
            LIMIT :top_k
            """
        ).bindparams(
            bindparam("embedding"),
            bindparam("user_id"),
            bindparam("source_type"),
            bindparam("top_k"),
        )

        result = await self._session.execute(
            sql,
            {
                "embedding": str(embedding),
                "user_id": user_id,
                "source_type": target_source_type.value,
                "top_k": top_k,
            },
        )
        rows = result.mappings().all()
        return [
            SimilarityHit(
                document_id=row["document_id"],
                title=row["title"],
                url=row["url"],
                score=float(row["score"]),
                chunk_preview=row["chunk_preview"],
            )
            for row in rows
        ]

    async def hybrid_search(
        self,
        *,
        user_id: int,
        embedding: list[float],
        keywords_query: str,
        target_source_type: SourceType,
        top_k: int,
        rrf_k: int = 60,
        candidate_pool: int = 50,
    ) -> list[SimilarityHit]:
        """벡터 + BM25 결합 검색 (Reciprocal Rank Fusion).

        - 각 검색에서 candidate_pool개 후보를 가져와서 rank 계산
        - RRF score = sum(1 / (rrf_k + rank))
        - rrf_k는 보통 60 (논문 기본값)
        """
        sql = text(
            """
            WITH vector_hits AS (
                SELECT DISTINCT ON (d.id)
                    d.id AS document_id,
                    d.title,
                    d.url,
                    c.content,
                    c.embedding <=> CAST(:embedding AS vector) AS distance,
                    ROW_NUMBER() OVER (ORDER BY c.embedding <=> CAST(:embedding AS vector)) AS rank
                FROM document_chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE d.user_id = :user_id
                  AND d.source_type = :source_type
                ORDER BY d.id, c.embedding <=> CAST(:embedding AS vector)
                LIMIT :pool
            ),
            keyword_hits AS (
                SELECT DISTINCT ON (d.id)
                    d.id AS document_id,
                    d.title,
                    d.url,
                    c.content,
                    ts_rank(c.content_tsv, plainto_tsquery('simple', :keywords)) AS bm25_score,
                    ROW_NUMBER() OVER (
                        ORDER BY ts_rank(c.content_tsv, plainto_tsquery('simple', :keywords)) DESC
                    ) AS rank
                FROM document_chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE d.user_id = :user_id
                  AND d.source_type = :source_type
                  AND c.content_tsv @@ plainto_tsquery('simple', :keywords)
                ORDER BY d.id, ts_rank(c.content_tsv, plainto_tsquery('simple', :keywords)) DESC
                LIMIT :pool
            ),
            fused AS (
                SELECT
                    COALESCE(v.document_id, k.document_id) AS document_id,
                    COALESCE(v.title, k.title) AS title,
                    COALESCE(v.url, k.url) AS url,
                    COALESCE(v.content, k.content) AS content,
                    COALESCE(1.0 / (:rrf_k + v.rank), 0) +
                    COALESCE(1.0 / (:rrf_k + k.rank), 0) AS rrf_score
                FROM vector_hits v
                FULL OUTER JOIN keyword_hits k ON v.document_id = k.document_id
            )
            SELECT document_id, title, url,
                   rrf_score AS score,
                   LEFT(content, 200) AS chunk_preview
            FROM fused
            ORDER BY rrf_score DESC
            LIMIT :top_k
            """
        )

        result = await self._session.execute(
            sql,
            {
                "embedding": str(embedding),
                "keywords": keywords_query,
                "user_id": user_id,
                "source_type": target_source_type.value,
                "pool": candidate_pool,
                "top_k": top_k,
                "rrf_k": rrf_k,
            },
        )
        rows = result.mappings().all()
        return [
            SimilarityHit(
                document_id=row["document_id"],
                title=row["title"],
                url=row["url"],
                score=float(row["score"]),
                chunk_preview=row["chunk_preview"],
            )
            for row in rows
        ]
