-- ECU Testing AI Platform — PostgreSQL Schema
-- PostgreSQL 15+
-- Run via: psql -d <dbname> -f schema_postgres.sql
-- Or use Alembic: alembic upgrade head

-- ─────────────────────────────────────────────
-- 1. Dataset and ingestion control
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS dataset_versions (
    id              BIGSERIAL PRIMARY KEY,
    name            VARCHAR(64)  NOT NULL UNIQUE,
    version_number  INTEGER,
    description     TEXT,
    source_path     TEXT         NOT NULL,
    status          VARCHAR(20)  NOT NULL DEFAULT 'active',
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (status IN ('active', 'deprecated', 'testing', 'archived'))
);

CREATE TABLE IF NOT EXISTS source_files (
    id                  BIGSERIAL PRIMARY KEY,
    dataset_version_id  BIGINT      NOT NULL REFERENCES dataset_versions(id) ON DELETE CASCADE,
    file_type           VARCHAR(40) NOT NULL,
    relative_path       TEXT        NOT NULL,
    file_name           TEXT        NOT NULL,
    file_hash           CHAR(64)    NOT NULL,
    file_size_bytes     BIGINT,
    file_modified_at    TIMESTAMPTZ,
    raw_json            JSONB       NOT NULL,
    imported_at         TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (dataset_version_id, relative_path, file_hash),
    CHECK (file_type IN ('capl_json', 'pytest_json', 'rag_text', 'rag_pdf', 'other'))
);

CREATE TABLE IF NOT EXISTS ingestion_jobs (
    id                  BIGSERIAL PRIMARY KEY,
    dataset_version_id  BIGINT      REFERENCES dataset_versions(id) ON DELETE SET NULL,
    job_type            VARCHAR(30) NOT NULL,
    status              VARCHAR(20) NOT NULL DEFAULT 'queued',
    triggered_by        VARCHAR(100),
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    files_discovered    INTEGER     NOT NULL DEFAULT 0,
    files_processed     INTEGER     NOT NULL DEFAULT 0,
    records_created     INTEGER     NOT NULL DEFAULT 0,
    records_updated     INTEGER     NOT NULL DEFAULT 0,
    records_skipped     INTEGER     NOT NULL DEFAULT 0,
    error_summary       TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (job_type IN ('full_refresh', 'incremental', 'validation', 'reindex_rag')),
    CHECK (status IN ('queued', 'running', 'completed', 'partial', 'failed'))
);

CREATE TABLE IF NOT EXISTS ingestion_job_items (
    id                BIGSERIAL PRIMARY KEY,
    ingestion_job_id  BIGINT      NOT NULL REFERENCES ingestion_jobs(id) ON DELETE CASCADE,
    source_file_id    BIGINT      REFERENCES source_files(id) ON DELETE SET NULL,
    relative_path     TEXT,
    status            VARCHAR(20) NOT NULL,
    detail            TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (status IN ('processed', 'skipped', 'failed'))
);

-- ─────────────────────────────────────────────
-- 2. Structured ECU input data
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS capl_documents (
    id                  BIGSERIAL PRIMARY KEY,
    dataset_version_id  BIGINT  NOT NULL REFERENCES dataset_versions(id) ON DELETE CASCADE,
    source_file_id      BIGINT  NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
    parsed_version      TEXT,
    dbc_root_key        TEXT,
    raw_json            JSONB   NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (dataset_version_id, source_file_id)
);

CREATE TABLE IF NOT EXISTS can_nodes (
    id                BIGSERIAL PRIMARY KEY,
    capl_document_id  BIGINT       NOT NULL REFERENCES capl_documents(id) ON DELETE CASCADE,
    node_name         VARCHAR(255) NOT NULL,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (capl_document_id, node_name)
);

CREATE TABLE IF NOT EXISTS can_messages (
    id                BIGSERIAL PRIMARY KEY,
    capl_document_id  BIGINT       NOT NULL REFERENCES capl_documents(id) ON DELETE CASCADE,
    frame_id          INTEGER      NOT NULL,
    frame_id_hex      VARCHAR(16),
    name              VARCHAR(255) NOT NULL,
    dlc               INTEGER,
    message_length    INTEGER,
    cycle_time_ms     INTEGER,
    senders           TEXT[]       NOT NULL DEFAULT '{}',
    receivers         TEXT[]       NOT NULL DEFAULT '{}',
    raw_payload       JSONB        NOT NULL,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (capl_document_id, frame_id, name)
);

CREATE TABLE IF NOT EXISTS can_signals (
    id              BIGSERIAL PRIMARY KEY,
    can_message_id  BIGINT       NOT NULL REFERENCES can_messages(id) ON DELETE CASCADE,
    name            VARCHAR(255) NOT NULL,
    start_bit       INTEGER,
    signal_length   INTEGER,
    byte_order      VARCHAR(20),
    is_signed       BOOLEAN,
    scale           DOUBLE PRECISION,
    signal_offset   DOUBLE PRECISION,
    minimum         DOUBLE PRECISION,
    maximum         DOUBLE PRECISION,
    unit            VARCHAR(64),
    receivers       TEXT[]       NOT NULL DEFAULT '{}',
    raw_payload     JSONB        NOT NULL,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (can_message_id, name, start_bit)
);

-- CAPL training examples: requirement_id + capl_script pairs from CAPL JSON
CREATE TABLE IF NOT EXISTS capl_scripts (
    id                BIGSERIAL PRIMARY KEY,
    capl_document_id  BIGINT       NOT NULL REFERENCES capl_documents(id) ON DELETE CASCADE,
    requirement_id    VARCHAR(120) NOT NULL,
    requirement_text  TEXT,
    capl_script       TEXT         NOT NULL,
    capl_pattern      VARCHAR(40),
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (capl_document_id, requirement_id)
);

CREATE TABLE IF NOT EXISTS requirements (
    id                  BIGSERIAL PRIMARY KEY,
    dataset_version_id  BIGINT       NOT NULL REFERENCES dataset_versions(id) ON DELETE CASCADE,
    source_file_id      BIGINT       NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
    requirement_id      VARCHAR(120) NOT NULL,
    title               VARCHAR(255),
    description         TEXT         NOT NULL,
    python_test_setup   TEXT,
    raw_json            JSONB        NOT NULL,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (dataset_version_id, requirement_id)
);

CREATE TABLE IF NOT EXISTS requirement_can_messages (
    id              BIGSERIAL PRIMARY KEY,
    requirement_id  BIGINT  NOT NULL REFERENCES requirements(id) ON DELETE CASCADE,
    node_name       VARCHAR(255),
    arbitration_id  VARCHAR(32),
    signal_name     VARCHAR(255),
    bit_position    INTEGER,
    raw_payload     JSONB   NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS test_cases (
    id                  BIGSERIAL PRIMARY KEY,
    requirement_id      BIGINT       NOT NULL REFERENCES requirements(id) ON DELETE CASCADE,
    test_case_id        VARCHAR(120) NOT NULL,
    title               VARCHAR(255) NOT NULL,
    precondition        TEXT,
    steps               JSONB        NOT NULL,
    expected_result     TEXT,
    python_test_script  TEXT,
    raw_json            JSONB        NOT NULL,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (requirement_id, test_case_id)
);

CREATE TABLE IF NOT EXISTS python_test_scripts (
    id              BIGSERIAL PRIMARY KEY,
    requirement_id  BIGINT      NOT NULL REFERENCES requirements(id) ON DELETE CASCADE,
    script_role     VARCHAR(30) NOT NULL,
    script_text     TEXT        NOT NULL,
    dependencies    JSONB,
    raw_json        JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (script_role IN ('setup', 'test_case', 'helper'))
);

-- ─────────────────────────────────────────────
-- 3. RAG data model
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS rag_documents (
    id                  BIGSERIAL PRIMARY KEY,
    dataset_version_id  BIGINT      NOT NULL REFERENCES dataset_versions(id) ON DELETE CASCADE,
    source_file_id      BIGINT      REFERENCES source_files(id) ON DELETE SET NULL,
    source_type         VARCHAR(40) NOT NULL,
    source_entity_type  VARCHAR(40) NOT NULL,
    source_entity_id    BIGINT,
    document_title      VARCHAR(255),
    document_text       TEXT        NOT NULL,
    document_hash       CHAR(64)    NOT NULL,
    metadata            JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (dataset_version_id, source_type, document_hash),
    CHECK (source_type IN (
        'capl_script', 'capl_message', 'capl_signal',
        'requirement', 'test_case',
        'python_setup', 'python_script', 'external_doc'
    )),
    CHECK (source_entity_type IN (
        'can_message', 'can_signal', 'capl_script',
        'requirement', 'test_case', 'script', 'file'
    ))
);

CREATE TABLE IF NOT EXISTS rag_chunks (
    id               BIGSERIAL PRIMARY KEY,
    rag_document_id  BIGINT  NOT NULL REFERENCES rag_documents(id) ON DELETE CASCADE,
    chunk_index      INTEGER NOT NULL,
    chunk_text       TEXT    NOT NULL,
    chunk_hash       CHAR(64) NOT NULL,
    token_count      INTEGER,
    metadata         JSONB   NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (rag_document_id, chunk_index),
    UNIQUE (chunk_hash)
);

CREATE TABLE IF NOT EXISTS rag_chunk_sync (
    id               BIGSERIAL PRIMARY KEY,
    rag_chunk_id     BIGINT       NOT NULL REFERENCES rag_chunks(id) ON DELETE CASCADE,
    vector_store     VARCHAR(30)  NOT NULL DEFAULT 'qdrant',
    collection_name  VARCHAR(100) NOT NULL,
    point_id         VARCHAR(128) NOT NULL,
    embedding_model  VARCHAR(120) NOT NULL,
    sync_status      VARCHAR(20)  NOT NULL DEFAULT 'pending',
    synced_at        TIMESTAMPTZ,
    error_message    TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (vector_store, collection_name, point_id),
    UNIQUE (rag_chunk_id, vector_store, collection_name),
    CHECK (sync_status IN ('pending', 'synced', 'failed', 'stale'))
);

-- ─────────────────────────────────────────────
-- 4. Future runtime outputs
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS generated_artifacts (
    id                        BIGSERIAL PRIMARY KEY,
    dataset_version_id        BIGINT  REFERENCES dataset_versions(id) ON DELETE SET NULL,
    requirement_id            BIGINT  REFERENCES requirements(id) ON DELETE SET NULL,
    generated_test_cases      JSONB,
    generated_capl_code       TEXT,
    generated_python_code     TEXT,
    llm_model                 VARCHAR(120),
    prompt_version            VARCHAR(50),
    status                    VARCHAR(20) NOT NULL DEFAULT 'success',
    generation_time_seconds   DOUBLE PRECISION,
    created_by                VARCHAR(100),
    created_at                TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (status IN ('success', 'partial', 'failed'))
);

CREATE TABLE IF NOT EXISTS generation_feedback (
    id                    BIGSERIAL PRIMARY KEY,
    generated_artifact_id BIGINT  NOT NULL REFERENCES generated_artifacts(id) ON DELETE CASCADE,
    feedback_score        INTEGER,
    feedback_text         TEXT,
    reviewer              VARCHAR(100),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (feedback_score BETWEEN 1 AND 5)
);

-- ─────────────────────────────────────────────
-- Indexes
-- ─────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_source_files_dataset_type
    ON source_files(dataset_version_id, file_type);

CREATE INDEX IF NOT EXISTS idx_source_files_hash
    ON source_files(file_hash);

CREATE INDEX IF NOT EXISTS idx_can_messages_frame_id
    ON can_messages(frame_id);

CREATE INDEX IF NOT EXISTS idx_can_messages_name
    ON can_messages(name);

CREATE INDEX IF NOT EXISTS idx_can_signals_name
    ON can_signals(name);

CREATE INDEX IF NOT EXISTS idx_capl_scripts_req_id
    ON capl_scripts(requirement_id);

CREATE INDEX IF NOT EXISTS idx_requirements_dataset_req
    ON requirements(dataset_version_id, requirement_id);

CREATE INDEX IF NOT EXISTS idx_test_cases_requirement
    ON test_cases(requirement_id);

CREATE INDEX IF NOT EXISTS idx_rag_documents_dataset_type
    ON rag_documents(dataset_version_id, source_type);

CREATE INDEX IF NOT EXISTS idx_rag_chunks_document
    ON rag_chunks(rag_document_id, chunk_index);

CREATE INDEX IF NOT EXISTS idx_rag_chunk_sync_status
    ON rag_chunk_sync(sync_status, vector_store);

CREATE INDEX IF NOT EXISTS idx_ingestion_jobs_dataset_status
    ON ingestion_jobs(dataset_version_id, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_requirements_raw_json_gin
    ON requirements USING GIN(raw_json);

CREATE INDEX IF NOT EXISTS idx_rag_documents_metadata_gin
    ON rag_documents USING GIN(metadata);

CREATE INDEX IF NOT EXISTS idx_rag_chunks_metadata_gin
    ON rag_chunks USING GIN(metadata);
