"""R1 uq_documents, R2 blog_diagnoses, R4 profile_embeddings

Revision ID: 0004_r1_r2_r4
Revises: 0003_crawl_metadata
Create Date: 2026-06-24
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "0004_r1_r2_r4"
down_revision = "0003_crawl_metadata"
branch_labels = None
depends_on = None

EMBEDDING_DIM = 768


def upgrade() -> None:
    # alembic_version.version_num 기본 크기(32)가 긴 revision ID를 수용 못하므로 확장
    op.execute("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(64)")

    # R1: (user_id, source_type, external_id) 유일성 제약
    # external_id가 NULL인 행은 제약 대상에서 제외된다(PostgreSQL NULL 동등 비교 특성)
    op.create_unique_constraint(
        "uq_documents_user_source_external",
        "documents",
        ["user_id", "source_type", "external_id"],
    )

    # R2: 6지표 진단 결과 테이블
    op.create_table(
        "blog_diagnoses",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger, nullable=False),
        sa.Column(
            "analysis_job_id",
            sa.BigInteger,
            sa.ForeignKey("analysis_jobs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("metrics", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("category_fit", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("strengths", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("weaknesses", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("result_embedding", Vector(EMBEDDING_DIM), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_blog_diagnoses_user_id", "blog_diagnoses", ["user_id"])

    # R4: 온보딩 프로필 임베딩 (Spring이 read-only로 접근)
    op.create_table(
        "profile_embeddings",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger, nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_profile_embeddings_user_id", "profile_embeddings", ["user_id"])

    # R3: Spring이 JPA DDL-auto:update로 생성하므로 Analyzer Alembic에서는 생성하지 않는다.
    # Spring: diagnosis_quotas 테이블은 Spring의 DDL-auto 또는 별도 Flyway 마이그레이션으로 처리.


def downgrade() -> None:
    op.drop_table("profile_embeddings")
    op.drop_table("blog_diagnoses")
    op.drop_constraint(
        "uq_documents_user_source_external",
        "documents",
        type_="unique",
    )
