from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.client.llm_client import LLMClient
from app.core.exceptions import LLMClientError, ValidationError
from app.core.logging import get_logger
from app.domain.models import ProfileEmbedding

logger = get_logger(__name__)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class ProfileEmbeddingService:
    def __init__(self, session: AsyncSession, llm_client: LLMClient) -> None:
        self._session = session
        self._llm = llm_client

    async def embed_and_store(self, user_id: int, profile_text: str) -> ProfileEmbedding:
        stripped = profile_text.strip()
        if len(stripped) < 20:
            raise ValidationError("profile_text must be at least 20 characters after stripping")

        profile_hash = _sha256(stripped)

        # 동일 프로필 해시가 이미 존재하면 재임베딩 없이 기존 row 반환
        existing = await self._session.scalar(
            select(ProfileEmbedding).where(
                ProfileEmbedding.user_id == user_id,
                ProfileEmbedding.profile_hash == profile_hash,
            ).limit(1)
        )
        if existing is not None:
            logger.info("profile embedding cache hit user_id=%s hash=%s", user_id, profile_hash[:8])
            return existing

        try:
            embeddings = await self._llm.embed([stripped])
        except Exception as exc:
            logger.error("profile embedding failed user_id=%s err=%s", user_id, exc)
            raise LLMClientError(f"embedding failed: {exc}") from exc

        if not embeddings or not embeddings[0]:
            raise LLMClientError("LLM returned empty embedding")

        record = ProfileEmbedding(
            user_id=user_id,
            embedding=embeddings[0],
            profile_hash=profile_hash,
        )
        self._session.add(record)
        await self._session.flush()
        await self._session.refresh(record)

        logger.info(
            "profile embedding stored user_id=%s hash=%s embedding_dim=%s",
            user_id, profile_hash[:8], len(embeddings[0]),
        )
        return record
