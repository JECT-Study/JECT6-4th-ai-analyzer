from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.api.dependencies import ConversationServiceDep
from app.domain.schemas import ChatRequest, ChatResponse

router = APIRouter(prefix="/v1/conversations", tags=["conversations"])


@router.post(
    "/messages",
    response_model=ChatResponse,
    summary="분석 결과 기반 대화",
)
async def send_message(
    request: ChatRequest,
    service: ConversationServiceDep,
) -> ChatResponse:
    return await service.chat(request)


@router.delete(
    "/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    response_class=Response,
    summary="대화 세션 초기화",
)
async def reset_session(
    session_id: str,
    service: ConversationServiceDep,
) -> None:
    await service.reset_session(session_id)
