from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.client.llm_client import LLMClient, get_llm_client
from app.core.database import get_session
from app.service.analysis_service import AnalysisService
from app.service.conversation_service import ConversationService
from app.service.document_service import DocumentService

SessionDep = Annotated[AsyncSession, Depends(get_session)]
LLMDep = Annotated[LLMClient, Depends(get_llm_client)]


def get_document_service(session: SessionDep, llm: LLMDep) -> DocumentService:
    return DocumentService(session, llm)


def get_analysis_service(session: SessionDep, llm: LLMDep) -> AnalysisService:
    return AnalysisService(session, llm)


def get_conversation_service(session: SessionDep, llm: LLMDep) -> ConversationService:
    return ConversationService(session, llm)


DocumentServiceDep = Annotated[DocumentService, Depends(get_document_service)]
AnalysisServiceDep = Annotated[AnalysisService, Depends(get_analysis_service)]
ConversationServiceDep = Annotated[
    ConversationService, Depends(get_conversation_service)
]
