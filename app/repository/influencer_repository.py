from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger

logger = get_logger(__name__)


async def upsert_influencer(
    session: AsyncSession,
    *,
    blog_url: str,
    influencer_name: str | None,
    blog_name: str | None,
    title: str | None,
    thumbnail_url: str | None,
    category: str | None,
) -> None:
    """blog_url 기준으로 influencer 테이블에 upsert (이미 존재하면 무시)."""
    stmt = text("""
        INSERT INTO influencer (influencer_name, blog_name, title, thumbnail_url, blog_url, category)
        VALUES (:influencer_name, :blog_name, :title, :thumbnail_url, :blog_url, :category)
        ON CONFLICT (blog_url) DO NOTHING
    """)
    await session.execute(stmt, {
        "influencer_name": influencer_name,
        "blog_name": blog_name,
        "title": title,
        "thumbnail_url": thumbnail_url,
        "blog_url": blog_url,
        "category": category,
    })
    logger.info(
        "influencer upsert blog_url=%s nickname=%s category=%s",
        blog_url, influencer_name, category,
    )
