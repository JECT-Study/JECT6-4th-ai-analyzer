from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.client.llm_client import LLMClient
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.domain.enums import SourceType
from app.domain.schemas import ChunkRequest
from app.repository.document_repository import DocumentRepository
from app.service.document_service import DocumentService

logger = get_logger(__name__)

_MAX_CONTENT_PER_POST = 3000
_SEPARATOR = "\n\n---\n\n"


class BlogSnapshotService:
    """N개 포스트 문서를 집계해 BLOG_SNAPSHOT 단일 문서로 만드는 서비스."""

    def __init__(self, session: AsyncSession, llm_client: LLMClient) -> None:
        self._document_service = DocumentService(session, llm_client)
        self._documents = DocumentRepository(session)

    async def _find_existing_snapshot(
        self, *, user_id: int, batch_id: str
    ) -> int | None:
        """batch_id에 해당하는 BLOG_SNAPSHOT 문서가 이미 존재하면 document_id를 반환."""
        existing = await self._documents.find_by_external_id(
            user_id, SourceType.BLOG_SNAPSHOT, f"blog_snapshot:{batch_id}"
        )
        return existing.id if existing else None

    async def create_snapshot(
        self,
        *,
        user_id: int,
        blog_id: int | None,
        batch_id: str,
        document_ids: list[int],
        correlation_id: str | None = None,
    ) -> tuple[int, list[int]]:
        """document_ids에 해당하는 포스트를 집계한 BLOG_SNAPSHOT 문서를 생성하고 (snapshot_document_id, source_document_ids)를 반환.

        발행 실패 후 재시도 시에도 동일 batch_id 스냅샷이 중복 생성되지 않도록
        기존 스냅샷이 있으면 재사용한다."""
        # 이미 생성된 스냅샷이 있으면 재사용 (RabbitMQ 발행 실패 재시도 중복 방지)
        existing_id = await self._find_existing_snapshot(user_id=user_id, batch_id=batch_id)
        if existing_id is not None:
            logger.info(
                "blog snapshot already exists, reusing batch_id=%s document_id=%s",
                batch_id, existing_id,
            )
            return existing_id, document_ids

        posts = []
        for doc_id in document_ids:
            doc = await self._documents.get_by_id(doc_id)
            if doc is not None:
                posts.append(doc)

        if not posts:
            raise NotFoundError(f"no source documents found for batch_id={batch_id}")

        parts = [
            f"## {doc.title}\n\n{doc.content[:_MAX_CONTENT_PER_POST]}"
            for doc in posts
        ]
        aggregated_content = _SEPARATOR.join(parts)
        aggregated_title = f"블로그 전체 분석 스냅샷 ({len(posts)}개 포스트)"

        metadata: dict = {
            "batch_id": batch_id,
            "post_count": len(posts),
            "source_document_ids": [str(d) for d in document_ids],
        }
        if blog_id is not None:
            metadata["blog_id"] = str(blog_id)
        if correlation_id is not None:
            metadata["correlation_id"] = correlation_id

        chunk_request = ChunkRequest(
            user_id=user_id,
            source_type=SourceType.BLOG_SNAPSHOT,
            title=aggregated_title,
            content=aggregated_content,
            external_id=f"blog_snapshot:{batch_id}",
            metadata=metadata,
        )

        response = await self._document_service.ingest_and_chunk(chunk_request)
        logger.info(
            "blog snapshot created batch_id=%s post_count=%s snapshot_document_id=%s",
            batch_id, len(posts), response.document_id,
        )
        return response.document_id, document_ids
