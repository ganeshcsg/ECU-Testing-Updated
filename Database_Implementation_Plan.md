# PostgreSQL Database Implementation Plan for ECU RAG and Input Data

## Goal
Implement the database layer for the ECU Testing AI platform using PostgreSQL as the system of record for:
- structured input data from `Data_V1` through `Data_V7`
- RAG source metadata and chunk tracking
- ingestion jobs, auditability, and dataset versioning
- future generated artifacts and feedback

This stage keeps PostgreSQL as the authoritative relational store and Qdrant as the vector index for semantic retrieval.

## Scope of This Stage
This document covers the database-layer stage only:
- define the PostgreSQL schema
- map current JSON inputs into relational tables
- define RAG chunk metadata tables for sync with Qdrant
- define ingestion workflow and refresh strategy
- define indexes, constraints, and operational rules

This stage does not require replacing Qdrant. It prepares PostgreSQL so the RAG service, data pipeline, and API layer can scale cleanly.

## Implementation Target in This Repo
The implementation for this plan should be done primarily by extending the existing application flow in:
- [complete_rag_appV1.0.py](c:\Users\ganesjad\Documents\ECU Testing POC\Final_Code_versions\ECU_V10\complete_rag_appV1.0.py)
- [rag_vector_store_qdrant.py](c:\Users\ganesjad\Documents\ECU Testing POC\Final_Code_versions\ECU_V10\rag_vector_store_qdrant.py)

This means the database layer should be integrated into the current Streamlit and Qdrant-based RAG flow, not designed as a separate greenfield implementation.

## Dataset Coverage in Current Repo
The current database-layer implementation should ingest all dataset folders present in this repo:
- `Data_V1`
- `Data_V2`
- `Data_V3`
- `Data_V4`
- `Data_V5`
- `Data_V6`
- `Data_V7`

The ingestion design should still remain future-safe for additional folders such as `Data_V8`, `Data_V9`, and later `Data_Vx`.

## Source Data Observed in This Repo

### CAPL/DBC input
Example file:
- `Data_V1/CAPL_Data_01.json`

Observed structure:
- top-level key: `DBC_outout_after_parsing`
- `version`
- `nodes`
- `messages`
- each message contains `frame_id`, `name`, `length`, `senders`, `signals`
- each signal contains `name`, `start`, `length`, `byte_order`, `is_signed`, `scale`, `offset`, `minimum`, `maximum`, `unit`, `receivers`

### Requirement and Python test input
Example file:
- `Data_V1/Python_Script_data_01/pytest_data_REQ_01.json`

Observed structure:
- top-level key: `requirement`
- `requirement_id`
- `description`
- `can_messages`
- `python_test_setup`
- top-level key: `test_cases`
- each test case contains `test_case_id`, `title`, `precondition`, `steps`, `expected_result`, `python_test_script`

## Database Design Principles
- PostgreSQL is the source of truth for all structured and audit data.
- Raw source JSON is preserved in `JSONB` columns for traceability and recovery.
- Dataset versioning is mandatory so `Data_V1` through `Data_V7` remain isolated and comparable.
- RAG vectors stay in Qdrant, but PostgreSQL stores the source document and chunk metadata.
- Every ingest operation is idempotent, hash-aware, and auditable.
- The schema supports incremental refresh for the current `Data_V1` to `Data_V7` range and future `Data_Vx` folders without code changes.

## Recommended Architecture Boundary

### PostgreSQL stores
- dataset versions
- source files and hashes
- parsed CAN structure
- requirements and test cases
- Python setup/test script text
- RAG documents and chunks
- Qdrant sync metadata
- ingestion job history
- generated outputs and feedback later

### Qdrant stores
- chunk embeddings
- nearest-neighbor search index
- lightweight payload fields needed for retrieval filters

### Sync contract
Each RAG chunk inserted into Qdrant should reference:
- `rag_chunk_id`
- `dataset_version`
- `source_type`
- `requirement_id` if applicable
- `message_name` if applicable
- `chunk_hash`

PostgreSQL remains the authoritative owner of chunk content and metadata.

## File-Level Integration Plan

### `complete_rag_appV1.0.py`
This file should be updated to:
- initialize PostgreSQL connectivity from environment configuration
- trigger dataset discovery for `Data_V1` through `Data_V7`
- call ingestion or sync routines before or during RAG initialization
- read structured metadata from PostgreSQL where appropriate
- continue using Qdrant retrieval for semantic search
- optionally expose refresh, validation, and import-status controls in the app UI

### `rag_vector_store_qdrant.py`
This file should be updated to:
- persist document and chunk metadata into PostgreSQL before or alongside Qdrant upsert
- store Qdrant sync metadata such as collection, point ID, embedding model, and sync status
- support rehydration of chunk content from PostgreSQL instead of relying only on in-memory state
- support stale chunk detection when source file hashes or chunk hashes change
- keep Qdrant focused on vectors and filter payloads while PostgreSQL owns source-of-truth metadata

### Recommended supporting modules
If needed, add small helper modules instead of overloading the two main files:
- `database/connection.py`
- `database/models.py`
- `database/repository.py`
- `database/ingestion.py`

The main application flow should still remain driven by `complete_rag_appV1.0.py` and `rag_vector_store_qdrant.py`.

## Logical Data Model

### 1. Dataset and ingestion control
- `dataset_versions`
- `source_files`
- `ingestion_jobs`
- `ingestion_job_items`

### 2. Structured ECU input data
- `capl_documents`
- `can_nodes`
- `can_messages`
- `can_signals`
- `requirements`
- `requirement_can_messages`
- `test_cases`
- `python_test_scripts`

### 3. RAG data model
- `rag_documents`
- `rag_chunks`
- `rag_chunk_sync`

### 4. Future runtime outputs
- `generated_artifacts`
- `generation_feedback`

## PostgreSQL Schema

```sql
CREATE TABLE dataset_versions (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(64) NOT NULL UNIQUE,
    version_number INTEGER,
    description TEXT,
    source_path TEXT NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (status IN ('active', 'deprecated', 'testing', 'archived'))
);

CREATE TABLE source_files (
    id BIGSERIAL PRIMARY KEY,
    dataset_version_id BIGINT NOT NULL REFERENCES dataset_versions(id) ON DELETE CASCADE,
    file_type VARCHAR(40) NOT NULL,
    relative_path TEXT NOT NULL,
    file_name TEXT NOT NULL,
    file_hash CHAR(64) NOT NULL,
    file_size_bytes BIGINT,
    file_modified_at TIMESTAMPTZ,
    raw_json JSONB NOT NULL,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (dataset_version_id, relative_path, file_hash),
    CHECK (file_type IN ('capl_json', 'pytest_json', 'rag_text', 'rag_pdf', 'other'))
);

CREATE TABLE ingestion_jobs (
    id BIGSERIAL PRIMARY KEY,
    dataset_version_id BIGINT REFERENCES dataset_versions(id) ON DELETE SET NULL,
    job_type VARCHAR(30) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'queued',
    triggered_by VARCHAR(100),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    files_discovered INTEGER NOT NULL DEFAULT 0,
    files_processed INTEGER NOT NULL DEFAULT 0,
    records_created INTEGER NOT NULL DEFAULT 0,
    records_updated INTEGER NOT NULL DEFAULT 0,
    records_skipped INTEGER NOT NULL DEFAULT 0,
    error_summary TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (job_type IN ('full_refresh', 'incremental', 'validation', 'reindex_rag')),
    CHECK (status IN ('queued', 'running', 'completed', 'partial', 'failed'))
);

CREATE TABLE ingestion_job_items (
    id BIGSERIAL PRIMARY KEY,
    ingestion_job_id BIGINT NOT NULL REFERENCES ingestion_jobs(id) ON DELETE CASCADE,
    source_file_id BIGINT REFERENCES source_files(id) ON DELETE SET NULL,
    relative_path TEXT,
    status VARCHAR(20) NOT NULL,
    detail TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (status IN ('processed', 'skipped', 'failed'))
);

CREATE TABLE capl_documents (
    id BIGSERIAL PRIMARY KEY,
    dataset_version_id BIGINT NOT NULL REFERENCES dataset_versions(id) ON DELETE CASCADE,
    source_file_id BIGINT NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
    parsed_version TEXT,
    dbc_root_key TEXT,
    raw_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (dataset_version_id, source_file_id)
);

CREATE TABLE can_nodes (
    id BIGSERIAL PRIMARY KEY,
    capl_document_id BIGINT NOT NULL REFERENCES capl_documents(id) ON DELETE CASCADE,
    node_name VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (capl_document_id, node_name)
);

CREATE TABLE can_messages (
    id BIGSERIAL PRIMARY KEY,
    capl_document_id BIGINT NOT NULL REFERENCES capl_documents(id) ON DELETE CASCADE,
    frame_id INTEGER NOT NULL,
    frame_id_hex VARCHAR(16),
    name VARCHAR(255) NOT NULL,
    dlc INTEGER,
    message_length INTEGER,
    cycle_time_ms INTEGER,
    senders TEXT[] NOT NULL DEFAULT '{}',
    receivers TEXT[] NOT NULL DEFAULT '{}',
    raw_payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (capl_document_id, frame_id, name)
);

CREATE TABLE can_signals (
    id BIGSERIAL PRIMARY KEY,
    can_message_id BIGINT NOT NULL REFERENCES can_messages(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    start_bit INTEGER,
    signal_length INTEGER,
    byte_order VARCHAR(20),
    is_signed BOOLEAN,
    scale DOUBLE PRECISION,
    offset DOUBLE PRECISION,
    minimum DOUBLE PRECISION,
    maximum DOUBLE PRECISION,
    unit VARCHAR(64),
    receivers TEXT[] NOT NULL DEFAULT '{}',
    raw_payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (can_message_id, name, start_bit)
);

CREATE TABLE requirements (
    id BIGSERIAL PRIMARY KEY,
    dataset_version_id BIGINT NOT NULL REFERENCES dataset_versions(id) ON DELETE CASCADE,
    source_file_id BIGINT NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
    requirement_id VARCHAR(120) NOT NULL,
    title VARCHAR(255),
    description TEXT NOT NULL,
    python_test_setup TEXT,
    raw_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (dataset_version_id, requirement_id)
);

CREATE TABLE requirement_can_messages (
    id BIGSERIAL PRIMARY KEY,
    requirement_id BIGINT NOT NULL REFERENCES requirements(id) ON DELETE CASCADE,
    node_name VARCHAR(255),
    arbitration_id VARCHAR(32),
    signal_name VARCHAR(255),
    bit_position INTEGER,
    raw_payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE test_cases (
    id BIGSERIAL PRIMARY KEY,
    requirement_id BIGINT NOT NULL REFERENCES requirements(id) ON DELETE CASCADE,
    test_case_id VARCHAR(120) NOT NULL,
    title VARCHAR(255) NOT NULL,
    precondition TEXT,
    steps JSONB NOT NULL,
    expected_result TEXT,
    python_test_script TEXT,
    raw_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (requirement_id, test_case_id)
);

CREATE TABLE python_test_scripts (
    id BIGSERIAL PRIMARY KEY,
    requirement_id BIGINT NOT NULL REFERENCES requirements(id) ON DELETE CASCADE,
    script_role VARCHAR(30) NOT NULL,
    script_text TEXT NOT NULL,
    dependencies JSONB,
    raw_json JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (script_role IN ('setup', 'test_case', 'helper'))
);

CREATE TABLE rag_documents (
    id BIGSERIAL PRIMARY KEY,
    dataset_version_id BIGINT NOT NULL REFERENCES dataset_versions(id) ON DELETE CASCADE,
    source_file_id BIGINT REFERENCES source_files(id) ON DELETE SET NULL,
    source_type VARCHAR(40) NOT NULL,
    source_entity_type VARCHAR(40) NOT NULL,
    source_entity_id BIGINT,
    document_title VARCHAR(255),
    document_text TEXT NOT NULL,
    document_hash CHAR(64) NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (dataset_version_id, source_type, document_hash),
    CHECK (source_type IN ('capl_message', 'capl_signal', 'requirement', 'test_case', 'python_setup', 'python_script', 'external_doc')),
    CHECK (source_entity_type IN ('can_message', 'can_signal', 'requirement', 'test_case', 'script', 'file'))
);

CREATE TABLE rag_chunks (
    id BIGSERIAL PRIMARY KEY,
    rag_document_id BIGINT NOT NULL REFERENCES rag_documents(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    chunk_text TEXT NOT NULL,
    chunk_hash CHAR(64) NOT NULL,
    token_count INTEGER,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (rag_document_id, chunk_index),
    UNIQUE (chunk_hash)
);

CREATE TABLE rag_chunk_sync (
    id BIGSERIAL PRIMARY KEY,
    rag_chunk_id BIGINT NOT NULL REFERENCES rag_chunks(id) ON DELETE CASCADE,
    vector_store VARCHAR(30) NOT NULL DEFAULT 'qdrant',
    collection_name VARCHAR(100) NOT NULL,
    point_id VARCHAR(128) NOT NULL,
    embedding_model VARCHAR(120) NOT NULL,
    sync_status VARCHAR(20) NOT NULL DEFAULT 'pending',
    synced_at TIMESTAMPTZ,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (vector_store, collection_name, point_id),
    UNIQUE (rag_chunk_id, vector_store, collection_name),
    CHECK (sync_status IN ('pending', 'synced', 'failed', 'stale'))
);

CREATE TABLE generated_artifacts (
    id BIGSERIAL PRIMARY KEY,
    dataset_version_id BIGINT REFERENCES dataset_versions(id) ON DELETE SET NULL,
    requirement_id BIGINT REFERENCES requirements(id) ON DELETE SET NULL,
    generated_test_cases JSONB,
    generated_capl_code TEXT,
    generated_python_code TEXT,
    llm_model VARCHAR(120),
    prompt_version VARCHAR(50),
    status VARCHAR(20) NOT NULL DEFAULT 'success',
    generation_time_seconds DOUBLE PRECISION,
    created_by VARCHAR(100),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (status IN ('success', 'partial', 'failed'))
);

CREATE TABLE generation_feedback (
    id BIGSERIAL PRIMARY KEY,
    generated_artifact_id BIGINT NOT NULL REFERENCES generated_artifacts(id) ON DELETE CASCADE,
    feedback_score INTEGER,
    feedback_text TEXT,
    reviewer VARCHAR(100),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (feedback_score BETWEEN 1 AND 5)
);
```

## Core Indexes

```sql
CREATE INDEX idx_source_files_dataset_type
    ON source_files(dataset_version_id, file_type);

CREATE INDEX idx_source_files_hash
    ON source_files(file_hash);

CREATE INDEX idx_can_messages_frame_id
    ON can_messages(frame_id);

CREATE INDEX idx_can_messages_name
    ON can_messages(name);

CREATE INDEX idx_can_signals_name
    ON can_signals(name);

CREATE INDEX idx_requirements_dataset_req
    ON requirements(dataset_version_id, requirement_id);

CREATE INDEX idx_test_cases_requirement
    ON test_cases(requirement_id);

CREATE INDEX idx_rag_documents_dataset_type
    ON rag_documents(dataset_version_id, source_type);

CREATE INDEX idx_rag_chunks_document
    ON rag_chunks(rag_document_id, chunk_index);

CREATE INDEX idx_rag_chunk_sync_status
    ON rag_chunk_sync(sync_status, vector_store);

CREATE INDEX idx_ingestion_jobs_dataset_status
    ON ingestion_jobs(dataset_version_id, status, created_at DESC);

CREATE INDEX idx_requirements_raw_json_gin
    ON requirements USING GIN(raw_json);

CREATE INDEX idx_rag_documents_metadata_gin
    ON rag_documents USING GIN(metadata);

CREATE INDEX idx_rag_chunks_metadata_gin
    ON rag_chunks USING GIN(metadata);
```

## How Input Data Maps into the Schema

### CAPL JSON mapping
`Data_V*/CAPL_Data_*.json`

- file row goes into `source_files`
- top-level parsed document goes into `capl_documents`
- each node in `DBC_outout_after_parsing.nodes` goes into `can_nodes`
- each message goes into `can_messages`
- each signal under a message goes into `can_signals`

### Pytest JSON mapping
`Data_V*/Python_Script_data_*/pytest_data_REQ_*.json`

- file row goes into `source_files`
- `requirement` object goes into `requirements`
- each item in `requirement.can_messages` goes into `requirement_can_messages`
- `requirement.python_test_setup` goes into `python_test_scripts` with `script_role='setup'`
- each object in `test_cases` goes into `test_cases`
- each `python_test_script` can also be duplicated into `python_test_scripts` with `script_role='test_case'` if separate script-level querying is useful

## How RAG Data Maps into the Schema

### RAG document creation rules
Create one `rag_documents` row for each retrievable semantic unit, for example:
- requirement description
- test case text
- Python setup block
- CAN message summary
- CAN signal summary

### Suggested text templates

#### Requirement document text
```text
Requirement ID: REQ_01
Description: The BCM shall transmit the current hazard light status on the CAN bus.
Related CAN Messages:
- Node: BCM, Arbitration ID: 0x100, Signal: Hazard_Light_Status, Bit: 0
Python Setup:
<python_test_setup text>
```

#### CAN message document text
```text
Message: BCM_Status_1
Frame ID: 256 (0x100)
Sender: BCM
Signals:
- Hazard_Light_Status bit 0 length 1
- Headlight_Status bit 1 length 2
...
```

### Chunking rules
- chunk size: 400 to 800 tokens
- overlap: 50 to 100 tokens
- never split a short requirement or a short test case unless needed
- preserve metadata such as `dataset_version`, `requirement_id`, `message_name`, `signal_name`, `source_type`

### Qdrant payload recommendation
```json
{
  "rag_chunk_id": 12345,
  "dataset_version": "Data_V1",
  "source_type": "requirement",
  "requirement_id": "REQ_01",
  "message_name": "BCM_Status_1",
  "chunk_hash": "..."
}
```

## Ingestion Workflow

### Step 1. Discover dataset versions
- scan workspace root for `Data_V*`
- create or update `dataset_versions`
- derive `version_number` from folder name when possible

### Step 2. Register source files
- discover `CAPL_Data_*.json`
- discover `Python_Script_data_*/pytest_data_REQ_*.json`
- compute SHA-256 hash
- insert into `source_files`
- skip unchanged files using `(dataset_version_id, relative_path, file_hash)`

### Step 3. Parse structured input data
- parse CAPL JSON into `capl_documents`, `can_nodes`, `can_messages`, `can_signals`
- parse pytest JSON into `requirements`, `requirement_can_messages`, `test_cases`, `python_test_scripts`

### Step 4. Build RAG documents
- generate normalized document text for each requirement, test case, message, and signal
- insert rows into `rag_documents`

### Step 5. Chunk for retrieval
- split each `rag_documents.document_text`
- insert rows into `rag_chunks`

### Step 6. Sync to Qdrant
- create embeddings
- upsert points into the Qdrant collection
- write point metadata into `rag_chunk_sync`

### Step 7. Audit and metrics
- update `ingestion_jobs`
- add per-file results into `ingestion_job_items`

## Refresh Strategy

### Full refresh
Use when:
- schema changes
- chunking logic changes
- prompt or document formatting changes significantly

Approach:
- create a new ingestion job
- delete rows for the dataset version from dependent tables using cascade-safe order
- re-import source files
- rebuild RAG documents and chunks
- resync Qdrant

### Incremental refresh
Use when:
- new files are added
- existing files changed

Approach:
- compare file hash
- only process changed files
- mark old chunk sync rows as `stale` if document hash changed
- re-upsert fresh chunks into Qdrant

### Validation refresh
Use when:
- checking for missing or partially imported records

Approach:
- compare discovered files vs `source_files`
- compare expected row counts vs actual row counts
- retry failed items from `ingestion_job_items`

## Recommended Delete/Reload Rule
For changed files, do not do blind table truncation. Delete by file lineage:
- delete `capl_documents` or `requirements` linked to the changed `source_file_id`
- let dependent rows cascade
- rebuild only that file’s structured data and RAG rows

This keeps refreshes fast and avoids damaging unrelated dataset data.

## Example Queries

### All requirements for one dataset version
```sql
SELECT r.requirement_id, r.description
FROM requirements r
JOIN dataset_versions dv ON dv.id = r.dataset_version_id
WHERE dv.name = 'Data_V1'
ORDER BY r.requirement_id;
```

### Find all CAN messages for a requirement
```sql
SELECT r.requirement_id, rcm.node_name, rcm.arbitration_id, rcm.signal_name
FROM requirements r
JOIN requirement_can_messages rcm ON rcm.requirement_id = r.id
WHERE r.requirement_id = 'REQ_01';
```

### Find RAG chunks backing a requirement
```sql
SELECT rc.id, rc.chunk_index, rc.chunk_text
FROM rag_chunks rc
JOIN rag_documents rd ON rd.id = rc.rag_document_id
WHERE rd.source_type = 'requirement'
  AND rd.metadata ->> 'requirement_id' = 'REQ_01'
ORDER BY rc.chunk_index;
```

### Find Qdrant sync failures
```sql
SELECT rcs.point_id, rcs.error_message, rd.document_title
FROM rag_chunk_sync rcs
JOIN rag_chunks rc ON rc.id = rcs.rag_chunk_id
JOIN rag_documents rd ON rd.id = rc.rag_document_id
WHERE rcs.sync_status = 'failed';
```

## Operational Recommendations

### PostgreSQL version
- PostgreSQL 15 or later

### Connection strategy
- application connections through SQLAlchemy
- pgBouncer for pooled production access

### Migration tool
- Alembic if using SQLAlchemy

### Backup
- daily logical dump for development
- WAL archiving plus snapshots for staging/production

### Observability
Track:
- ingest duration
- files changed
- rows written by table
- chunk counts
- Qdrant sync failures

## Minimal Implementation Sequence
1. Add PostgreSQL configuration and connection handling for `complete_rag_appV1.0.py`.
2. Create database schema and migration baseline.
3. Implement `dataset_versions`, `source_files`, and `ingestion_jobs`.
4. Implement structured input tables for CAPL and pytest data.
5. Build the ingestion loader for all current datasets from `Data_V1` through `Data_V7`.
6. Extend `rag_vector_store_qdrant.py` so every indexed document and chunk is also tracked in PostgreSQL.
7. Update `complete_rag_appV1.0.py` to initialize or refresh the database-backed RAG metadata before retrieval.
8. Add validation queries and refresh commands.

## Recommended File Deliverables for This Stage
- `database/schema_postgres.sql`
- `database/alembic/` migrations
- `database/connection.py`
- `database/models.py`
- `database/repository.py`
- `database/ingestion.py`
- `.env` entries for `DATABASE_URL`, `QDRANT_HOST`, `QDRANT_PORT`, `QDRANT_COLLECTION`

## Practical Change Summary for Existing Files

### Changes expected in `complete_rag_appV1.0.py`
- load PostgreSQL settings from `.env`
- ensure dataset ingestion is performed for `Data_V1` to `Data_V7`
- fetch version, requirement, and chunk metadata from PostgreSQL
- provide a path to refresh or validate database and Qdrant sync

### Changes expected in `rag_vector_store_qdrant.py`
- add PostgreSQL-backed persistence for documents and chunks
- add `rag_chunk_sync` writes after Qdrant upserts
- support metadata lookup and recovery from PostgreSQL
- reduce dependence on process-local `_id_to_content` for long-term persistence

## What This Solves
- moves the project from file-only data handling across `Data_V1` to `Data_V7` into a scalable database layer
- keeps actual ECU input structure queryable in SQL
- gives RAG a stable metadata backbone
- supports versioned ingestion for `Data_V1` through `Data_V7` and future `Data_Vx`
- makes refresh, audit, and troubleshooting much easier

## Recommendation
For this project, use PostgreSQL for all structured and metadata storage, and keep Qdrant for embeddings. That is the cleanest database-layer stage for the current architecture because it improves scale and traceability without forcing a vector-database migration.

---

## Implementation Notes (as-built deviations and additions)

### `capl_scripts` table (added beyond original plan)

A `capl_scripts` table was added to the schema to store individual CAPL code blocks extracted
from each `CAPL_Data_*.json` file's `requirements` array.  Each row holds one CAPL script tied
to a specific requirement ID found inside the CAPL JSON, with pattern detection
(`cyclic_timer`, `reactive_message`, `reactive_key`, `on_start`, `variables`).

```sql
CREATE TABLE capl_scripts (
    id BIGSERIAL PRIMARY KEY,
    capl_document_id BIGINT NOT NULL REFERENCES capl_documents(id) ON DELETE CASCADE,
    requirement_id_text VARCHAR(120),
    description TEXT,
    capl_code TEXT NOT NULL,
    capl_pattern VARCHAR(40),
    raw_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (capl_pattern IN ('cyclic_timer','reactive_message','reactive_key','on_start','variables','unknown'))
);
```

The `rag_documents.source_type` CHECK constraint was extended to include `'capl_script'`.

### `app_runtime` dataset version

A special `dataset_versions` row named `'app_runtime'` (version_number=0) is created
automatically on first startup by `pg_bridge.init_for_app()`.  All `rag_documents` rows
written live by the Streamlit app during a session (via `RAGVectorStore._pg_write()`) are
owned by this version rather than by any `Data_V*` folder version.  This separates batch-ingested
data from runtime-added data and prevents the structural sync from overwriting runtime rows.

### Dual ingestion architecture

Two distinct paths write to PostgreSQL:

| Path | Trigger | Module | Qdrant |
|---|---|---|---|
| Batch structural sync | App startup (first boot only, source_files empty) | `services/data_pipeline/ingest_postgres.py` | Skipped (`skip_qdrant=True`) |
| Live runtime writes | Every `_add_document` / `add()` / `add_test_case` / `add_python_script` call | `rag_vector_store_qdrant.RAGVectorStore._pg_write()` | Already written (call comes after upsert) |

The batch path runs `run_ingestion()` with `skip_qdrant=True` so only the relational tables are
populated.  Qdrant is loaded separately by the normal `load_data_v1_into_rag()` call in
`complete_rag_appV1.0.py`, which triggers live `_pg_write()` calls for each chunk as it is
added to Qdrant.

### Actual file deliverables

| File | Status |
|---|---|
| `database/schema_postgres.sql` | Created — 18 tables + 14 indexes |
| `database/alembic/versions/0001_initial_schema.py` | Created |
| `database/alembic/env.py` | Created |
| `database/alembic.ini` | Created |
| `services/data_pipeline/db.py` | Created — psycopg2 connection factory for batch pipeline |
| `services/data_pipeline/ingest_postgres.py` | Created — 7-step ingestion pipeline |
| `services/data_pipeline/rag_sync.py` | Created — standalone Qdrant embedding sync |
| `services/data_pipeline/validators.py` | Created — row-count and sync-health reports |
| `services/data_pipeline/pg_bridge.py` | Created — app-facing init + artifact recording |
| `rag_vector_store_qdrant.py` | Modified — `attach_db()`, `_pg_write()`, `add_test_case`, `add_python_script` |
| `complete_rag_appV1.0.py` | Modified — DB init before RAG load, artifact recording |

### chunk_hash collision avoidance

`_pg_write()` computes `chunk_hash` as `SHA-256(f"{dataset_version_id}:{source_type}:{text}")`.
This ensures that identical text indexed under different source types (e.g., a CAPL script
block that matches a requirement description exactly) gets separate `rag_chunks` rows rather
than a silent hash collision on the `UNIQUE (chunk_hash)` constraint.

### PostgreSQL setup (Windows)

If `psql` is not on PATH, use one of these options:

1. **pgAdmin 4** — GUI tool bundled with the PostgreSQL Windows installer.
2. **Docker** — `docker run -e POSTGRES_PASSWORD=secret -e POSTGRES_USER=ecu_user -e POSTGRES_DB=ecu_testing -p 5432:5432 postgres:15`
3. **PostgreSQL Windows installer** — https://www.postgresql.org/download/windows/ (adds `psql` to PATH after install)

After creating the database, set `.env`:
```
DATABASE_URL=postgresql+psycopg2://ecu_user:secret@localhost:5432/ecu_testing
```

Then run Alembic migrations:
```bash
cd database
alembic upgrade head
```

Or let the app auto-apply the schema on first startup via `pg_bridge.ensure_schema()`.

---

Document version: 2.1
Target stage: Database Layer (as-built)
Applies to: RAG data + structured input data (`Data_V1` to `Data_V7`)
Recommended review point: before implementing ingestion service and migrations
