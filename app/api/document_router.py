from fastapi import APIRouter, status

from app.api.dependencies import DocumentServiceDep
from app.domain.schemas import (
    ChunkRequest,
    ChunkResponse,
    SimilarityMatchRequest,
    SimilarityMatchResponse,
)

router = APIRouter(prefix="/v1/documents", tags=["documents"])


@router.post(
    "/chunks",
    response_model=ChunkResponse,
    status_code=status.HTTP_201_CREATED,
    summary="문서 청킹 + 임베딩 저장",
)
async def create_chunks(
    request: ChunkRequest,
    service: DocumentServiceDep,
) -> ChunkResponse:
    """크롤러가 호출하는 진입점.

    external_id가 있으면 동일 문서를 갱신(기존 청크 삭제 후 재생성).
    """
    return await service.ingest_and_chunk(request)


@router.post(
    "/similarity",
    response_model=SimilarityMatchResponse,
    summary="유사 문서 검색",
)
async def search_similarity(
    request: SimilarityMatchRequest,
    service: DocumentServiceDep,
) -> SimilarityMatchResponse:
    """외부 글/공고 텍스트와 유사한 본인 블로그 글을 찾는다."""
    return await service.find_similar(request)
