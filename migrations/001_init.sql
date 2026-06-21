-- =============================================================================
-- [데모 / 수동 초기화 전용]
--
-- 이 파일은 로컬 개발 환경에서 빈 DB를 빠르게 세팅하거나
-- 인프라 없이 수동으로 스키마를 확인할 때만 사용한다.
--
-- 운영 및 CI 환경의 공식 마이그레이션 경로는 Alembic이다.
--   alembic upgrade head
--
-- Alembic 체인: 0001_init → 0002_tsvector → 0003_crawl_metadata
--
-- ⚠ 이 파일은 Alembic 체인과 수동으로 동기화해야 한다.
--    Alembic 리비전을 추가할 때 이 파일도 함께 갱신하거나,
--    갱신하지 않을 경우 이 파일이 오래된 스키마임을 인지하고 사용할 것.
-- =============================================================================

-- pgvector 확장
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    source_type VARCHAR(32) NOT NULL,
    external_id VARCHAR(255),
    url VARCHAR(2048),
    title VARCHAR(512) NOT NULL,
    content TEXT NOT NULL,
    doc_metadata JSONB NOT NULL DEFAULT '{}',
    content_hash VARCHAR(64),
    crawled_at TIMESTAMPTZ,
    ingestion_status VARCHAR(32) NOT NULL DEFAULT 'completed',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_documents_user_source ON documents (user_id, source_type);
CREATE INDEX IF NOT EXISTS ix_documents_external_id ON documents (external_id);

CREATE TABLE IF NOT EXISTS document_chunks (
    id BIGSERIAL PRIMARY KEY,
    document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index INT NOT NULL,
    content TEXT NOT NULL,
    token_count INT NOT NULL,
    embedding vector(768) NOT NULL,
    content_tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_chunks_document_id ON document_chunks (document_id);
-- HNSW 인덱스 (cosine)
CREATE INDEX IF NOT EXISTS ix_chunks_embedding_hnsw
    ON document_chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS ix_chunks_content_tsv
    ON document_chunks USING GIN (content_tsv);

CREATE TABLE IF NOT EXISTS analysis_jobs (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    result JSONB NOT NULL DEFAULT '{}',
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_analysis_user_id ON analysis_jobs (user_id);
CREATE INDEX IF NOT EXISTS ix_analysis_document_id ON analysis_jobs (document_id);
