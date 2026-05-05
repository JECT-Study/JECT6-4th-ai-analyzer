"""add crawl metadata to documents

Revision ID: 0003_crawl_metadata
Revises: 0002_tsvector
Create Date: 2026-05-06
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_crawl_metadata"
down_revision = "0002_tsvector"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("content_hash", sa.String(64), nullable=True))
    op.add_column(
        "documents", sa.Column("crawled_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "documents",
        sa.Column(
            "ingestion_status",
            sa.String(32),
            nullable=False,
            server_default="completed",
        ),
    )
    op.alter_column("documents", "ingestion_status", server_default=None)


def downgrade() -> None:
    op.drop_column("documents", "ingestion_status")
    op.drop_column("documents", "crawled_at")
    op.drop_column("documents", "content_hash")
