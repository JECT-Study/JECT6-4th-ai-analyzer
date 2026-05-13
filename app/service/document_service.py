from sqlalchemy.ext.asyncio import AsyncSession

from app.client.llm_client import LLMClient
from app.client.redis_client import get_redis
from app.core.config import get_settings
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.domain.models import Document, DocumentChunk
from app.domain.schemas import (
    ChunkRequest,
    ChunkResponse,
)
from app.repository.document_repository import DocumentRepository
from app.repository.embedding_cache import EmbeddingCache
from app.service.chunker import TextChunker

logger = get_logger(__name__)


class DocumentService:
    """청킹/임베딩 저장을 담당하는 서비스."""

    def __init__(
        self,
        session: AsyncSession,
        llm_client: LLMClient,
        embedding_cache: EmbeddingCache | None = None,
    ) -> None:
        self._session = session
        self._llm = llm_client
        self._documents = DocumentRepository(session)
        # NOTE(2026-05-13): 유사도 검색은 Spring 메인 서버 책임으로 이동했다.
        # 이전 구현:
        # self._similarity = SimilarityRepository(session)
        # self._rewriter = QueryRewriter(llm_client)
        settings = get_settings()
        self._chunker = TextChunker(
            chunk_size=settings.chunk_size_tokens,
            overlap=settings.chunk_overlap_tokens,
        )
        self._embedding_cache = embedding_cache or EmbeddingCache(
            get_redis(), model=settings.embedding_model
        )

    async def ingest_and_chunk(self, request: ChunkRequest) -> ChunkResponse:
        """문서 저장 → 청킹 → 임베딩 → 청크 저장.

        external_id가 있으면 upsert 방식으로 처리(기존 청크 삭제 후 재생성).
        """
        document = await self._upsert_document(request)
        text_chunks = self._chunker.chunk(request.content)

        if not text_chunks:
            logger.warning("no chunks produced for document_id=%s", document.id)
            return ChunkResponse(document_id=document.id, chunk_count=0)

        embeddings = await self._embed_with_cache(
            [c.content for c in text_chunks]
        )

        chunk_entities = [
            DocumentChunk(
                document_id=document.id,
                chunk_index=tc.index,
                content=tc.content,
                token_count=tc.token_count,
                embedding=emb,
            )
            for tc, emb in zip(text_chunks, embeddings, strict=True)
        ]
        await self._documents.add_chunks(chunk_entities)

        logger.info(
            "ingested document_id=%s chunks=%s", document.id, len(chunk_entities)
        )
        return ChunkResponse(document_id=document.id, chunk_count=len(chunk_entities))

    # NOTE(2026-05-13): 유사도 검색은 Spring 메인 서버가 Vector DB를 직접 조회하는
    # 책임으로 이동했다. 이전 구현은 이력 보존을 위해 주석으로 남긴다.
    #
    # async def find_similar(
    #     self, request: SimilarityMatchRequest
    # ) -> SimilarityMatchResponse:
    #     """쿼리 텍스트와 유사한 문서를 target_source_type 안에서 찾는다.
    #
    #     - use_hyde + query_source_type: 가상 본문으로 변환한 텍스트로 임베딩
    #     - use_hybrid: 벡터 + BM25(RRF) 결합. BM25는 항상 원본 키워드 사용
    #     """
    #     # HyDE는 임베딩 쿼리에만 적용. BM25 키워드는 원문 유지가 유리.
    #     embedding_query = request.query_text
    #     rewritten: str | None = None
    #     if request.use_hyde and request.query_source_type:
    #         rewritten = await self._rewriter.rewrite(
    #             request.query_source_type, request.query_text
    #         )
    #         if rewritten and rewritten != request.query_text:
    #             embedding_query = rewritten
    #         else:
    #             rewritten = None
    #
    #     embeddings = await self._embed_with_cache([embedding_query])
    #     embedding = embeddings[0]
    #
    #     if request.use_hybrid:
    #         keywords = request.keywords or request.query_text
    #         hits = await self._similarity.hybrid_search(
    #             user_id=request.user_id,
    #             embedding=embedding,
    #             keywords_query=keywords,
    #             target_source_type=request.target_source_type,
    #             top_k=request.top_k,
    #         )
    #     else:
    #         hits = await self._similarity.search_similar_documents(
    #             user_id=request.user_id,
    #             embedding=embedding,
    #             target_source_type=request.target_source_type,
    #             top_k=request.top_k,
    #         )
    #
    #     return SimilarityMatchResponse(
    #         matches=[
    #             SimilarDocument(
    #                 document_id=h.document_id,
    #                 title=h.title,
    #                 url=h.url,
    #                 score=round(h.score, 4),
    #                 matched_chunk_preview=h.chunk_preview,
    #             )
    #             for h in hits
    #         ],
    #         rewritten_query=rewritten,
    #     )

    async def _upsert_document(self, request: ChunkRequest) -> Document:
        existing: Document | None = None
        if request.external_id:
            existing = await self._documents.find_by_external_id(
                request.user_id, request.source_type, request.external_id
            )

        if existing:
            existing.title = request.title
            existing.content = request.content
            existing.url = request.url
            existing.doc_metadata = request.metadata
            existing.content_hash = request.content_hash
            existing.crawled_at = request.crawled_at
            existing.ingestion_status = request.ingestion_status
            await self._documents.delete_chunks_by_document(existing.id)
            return existing

        return await self._documents.create(
            Document(
                user_id=request.user_id,
                source_type=request.source_type,
                external_id=request.external_id,
                url=request.url,
                title=request.title,
                content=request.content,
                doc_metadata=request.metadata,
                content_hash=request.content_hash,
                crawled_at=request.crawled_at,
                ingestion_status=request.ingestion_status,
            )
        )

    async def get_document(self, document_id: int) -> Document:
        doc = await self._documents.get_by_id(document_id)
        if doc is None:
            raise NotFoundError(f"document not found: {document_id}")
        return doc

    async def _embed_with_cache(self, texts: list[str]) -> list[list[float]]:
        """캐시 히트는 그대로, 미스만 LLM 호출 후 캐시에 저장."""
        cached, miss_indexes = await self._embedding_cache.get_many(texts)

        if not miss_indexes:
            logger.info("embedding cache hit all (n=%s)", len(texts))
            return [emb for emb in cached if emb is not None]

        miss_texts = [texts[i] for i in miss_indexes]
        new_embeddings = await self._llm.embed(miss_texts)
        await self._embedding_cache.set_many(miss_texts, new_embeddings)

        # 결과 합치기
        result: list[list[float]] = []
        miss_iter = iter(zip(miss_indexes, new_embeddings, strict=True))
        next_miss_idx, next_miss_emb = next(miss_iter, (None, None))
        for i, emb in enumerate(cached):
            if emb is not None:
                result.append(emb)
            else:
                assert i == next_miss_idx
                result.append(next_miss_emb)
                next_miss_idx, next_miss_emb = next(miss_iter, (None, None))

        logger.info(
            "embedding cache hit=%s miss=%s",
            len(texts) - len(miss_indexes),
            len(miss_indexes),
        )
        return result
