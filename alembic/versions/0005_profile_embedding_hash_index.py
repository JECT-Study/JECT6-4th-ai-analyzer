"""profile_embeddings: profile_hash 컬럼, (user_id, created_at) 복합 인덱스, 유니크 제약 추가

Revision ID: 0005_profile_embedding_hash_index
Revises: 0004_r1_r2_r4
Create Date: 2026-06-24
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_profile_embedding_hash_index"
down_revision = "0004_r1_r2_r4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 동일 프로필 텍스트 재임베딩 방지를 위한 SHA-256 해시 컬럼
    op.add_column(
        "profile_embeddings",
        sa.Column("profile_hash", sa.String(64), nullable=True),
    )

    # (user_id, created_at) 복합 인덱스 — ORDER BY created_at DESC LIMIT 1 최적화
    op.create_index(
        "ix_profile_embeddings_user_created",
        "profile_embeddings",
        ["user_id", "created_at"],
    )

    # (user_id, profile_hash) 유니크 제약 — NULL 값은 PostgreSQL에서 제약 대상 외
    op.create_unique_constraint(
        "uq_profile_embeddings_user_hash",
        "profile_embeddings",
        ["user_id", "profile_hash"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_profile_embeddings_user_hash", "profile_embeddings", type_="unique")
    op.drop_index("ix_profile_embeddings_user_created", table_name="profile_embeddings")
    op.drop_column("profile_embeddings", "profile_hash")
