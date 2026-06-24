from __future__ import annotations

from fastapi import APIRouter

from app.api.dependencies import LLMDep, SessionDep
from app.domain.schemas import ProfileEmbedRequest, ProfileEmbedResponse
from app.service.profile_embedding_service import ProfileEmbeddingService

router = APIRouter(prefix="/v1/profile", tags=["profile"])


@router.post("/embed", response_model=ProfileEmbedResponse)
async def embed_profile(
    request: ProfileEmbedRequest,
    session: SessionDep,
    llm: LLMDep,
) -> ProfileEmbedResponse:
    service = ProfileEmbeddingService(session, llm)
    record = await service.embed_and_store(request.user_id, request.profile_text)
    return ProfileEmbedResponse(
        user_id=record.user_id,
        stored=True,
        has_embedding=True,
    )
