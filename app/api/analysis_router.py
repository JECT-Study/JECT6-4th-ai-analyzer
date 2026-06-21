from __future__ import annotations

from fastapi import APIRouter

from app.api.dependencies import AnalysisServiceDep
from app.domain.schemas import AnalysisJobResponse, AnalysisRequest

router = APIRouter(prefix="/v1/analysis", tags=["analysis"])


@router.post(
    "",
    response_model=AnalysisJobResponse,
    summary="블로그 글 분석 (동기 실행)",
)
async def run_analysis(
    request: AnalysisRequest,
    service: AnalysisServiceDep,
) -> AnalysisJobResponse:
    """직접 호출용. 보통은 큐 워커 경로를 쓰지만 즉시 실행이 필요할 때 사용."""
    job = await service.analyze(request)
    return AnalysisJobResponse.model_validate(job)


@router.get(
    "/documents/{document_id}",
    response_model=AnalysisJobResponse,
    summary="문서의 최근 분석 결과 조회",
)
async def get_analysis(
    document_id: int,
    service: AnalysisServiceDep,
) -> AnalysisJobResponse:
    job = await service.get_analysis_for_document(document_id)
    return AnalysisJobResponse.model_validate(job)
