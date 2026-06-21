from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import AnalysisStatus
from app.domain.models import AnalysisJob


class AnalysisJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, user_id: int, document_id: int) -> AnalysisJob:
        job = AnalysisJob(user_id=user_id, document_id=document_id)
        self._session.add(job)
        await self._session.flush()
        return job

    async def get_by_id(self, job_id: int) -> AnalysisJob | None:
        return await self._session.get(AnalysisJob, job_id)

    async def get_latest_by_document(self, document_id: int) -> AnalysisJob | None:
        stmt = (
            select(AnalysisJob)
            .where(AnalysisJob.document_id == document_id)
            .order_by(AnalysisJob.created_at.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def update_status(
        self,
        job: AnalysisJob,
        status: AnalysisStatus,
        *,
        result: dict | None = None,
        error_message: str | None = None,
    ) -> None:
        job.status = status
        if result is not None:
            job.result = result
        if error_message is not None:
            job.error_message = error_message
        await self._session.flush()
