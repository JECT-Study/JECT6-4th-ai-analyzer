from fastapi import APIRouter, status

from app.api.dependencies import CrawlServiceDep
from app.domain.schemas import CrawlJobRequest, CrawlJobResponse

router = APIRouter(prefix="/v1/crawl", tags=["crawl"])


@router.post(
    "/jobs",
    response_model=CrawlJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="크롤링 작업 등록",
)
async def create_crawl_job(
    request: CrawlJobRequest,
    service: CrawlServiceDep,
) -> CrawlJobResponse:
    """URL을 내부 Redis Streams 파이프라인에 등록한다."""
    return await service.enqueue(request)
