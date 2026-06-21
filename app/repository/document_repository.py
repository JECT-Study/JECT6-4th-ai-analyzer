from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import Document, DocumentChunk
from app.domain.enums import SourceType


class DocumentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, document: Document) -> Document:
        self._session.add(document)
        await self._session.flush()
        return document

    async def get_by_id(self, document_id: int) -> Document | None:
        return await self._session.get(Document, document_id)

    async def find_by_external_id(
        self, user_id: int, source_type: SourceType, external_id: str
    ) -> Document | None:
        stmt = select(Document).where(
            Document.user_id == user_id,
            Document.source_type == source_type,
            Document.external_id == external_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def add_chunks(self, chunks: list[DocumentChunk]) -> None:
        self._session.add_all(chunks)
        await self._session.flush()

    async def delete_chunks_by_document(self, document_id: int) -> None:
        from sqlalchemy import delete

        await self._session.execute(
            delete(DocumentChunk).where(DocumentChunk.document_id == document_id)
        )
