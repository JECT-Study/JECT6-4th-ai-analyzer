"""add tsvector for hybrid search

Revision ID: 0002_tsvector
Revises: 0001_init
Create Date: 2026-04-25
"""
from __future__ import annotations

from alembic import op

revision = "0002_tsvector"
down_revision = "0001_init"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # GENERATED 컬럼으로 자동 유지. 한국어는 simple config (단순 토큰화).
    # 한국어 형태소 분석이 필요하면 추가로 mecab 기반 ts config을 설치해 사용.
    op.execute(
        """
        ALTER TABLE document_chunks
        ADD COLUMN content_tsv tsvector
        GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED
        """
    )
    op.execute(
        """
        CREATE INDEX ix_chunks_content_tsv
        ON document_chunks USING GIN (content_tsv)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chunks_content_tsv")
    op.execute("ALTER TABLE document_chunks DROP COLUMN IF EXISTS content_tsv")
