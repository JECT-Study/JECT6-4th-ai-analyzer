from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import LLMDep, SessionDep
from app.client.llm_client import LLMClient
from app.service.blog_diagnosis_service import BlogDiagnosisService

router = APIRouter(prefix="/v1/diagnosis", tags=["diagnosis"])


class DiagnoseRequest(BaseModel):
    user_id: int
    document_id: int


class DiagnoseResponse(BaseModel):
    id: int
    user_id: int
    metrics: dict
    category_fit: list
    strengths: list
    weaknesses: list
    has_embedding: bool


@router.post("", response_model=DiagnoseResponse)
async def diagnose(
    request: DiagnoseRequest,
    session: SessionDep,
    llm: LLMDep,
) -> DiagnoseResponse:
    service = BlogDiagnosisService(session, llm)
    diag = await service.diagnose(request.user_id, request.document_id)
    return DiagnoseResponse(
        id=diag.id,
        user_id=diag.user_id,
        metrics=diag.metrics,
        category_fit=diag.category_fit,
        strengths=diag.strengths,
        weaknesses=diag.weaknesses,
        has_embedding=diag.result_embedding is not None,
    )
