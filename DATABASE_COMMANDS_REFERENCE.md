# Database Commands Reference — ECU Testing POC

## Connection Details

| Field      | Value                                                          |
|------------|----------------------------------------------------------------|
| Host       | `localhost`                                                    |
| Port       | `5432`                                                         |
| Database   | `ecu_testing`                                                  |
| Username   | `ecu_user`                                                     |
| Password   | `secret`                                                       |
| Full URL   | `postgresql+psycopg2://ecu_user:secret@localhost:5432/ecu_testing` |

PostgreSQL runs inside the Docker container named **`ecu-postgres`**.

---

## Docker — Container Management

```bash
# Check if container is running
docker ps

# Start the container (if stopped)
docker start ecu-postgres

# Stop the container
docker stop ecu-postgres

# View container logs
docker logs ecu-postgres
```

---

## Accessing the Database via Docker

### Open an interactive psql shell inside the container
```bash
docker exec -it ecu-postgres psql -U ecu_user -d ecu_testing
```

### Run a one-liner query without entering the container
```bash
docker exec ecu-postgres psql -U ecu_user -d ecu_testing -c "YOUR SQL HERE;"
```

---

## psql Navigation Commands (inside the shell)

| Command | Description                  |
|---------|------------------------------|
| `\dt`   | List all tables              |
| `\d tablename` | Show table schema     |
| `\l`    | List all databases           |
| `\q`    | Exit psql                    |
| `\x`    | Toggle expanded display      |

---

## Useful Queries

### Check all tables exist
```sql
\dt
```

### Row counts across all key tables
```sql
SELECT 'dataset_versions'    AS table_name, COUNT(*) AS row_count FROM dataset_versions
UNION ALL SELECT 'source_files',        COUNT(*) FROM source_files
UNION ALL SELECT 'can_messages',        COUNT(*) FROM can_messages
UNION ALL SELECT 'can_signals',         COUNT(*) FROM can_signals
UNION ALL SELECT 'requirements',        COUNT(*) FROM requirements
UNION ALL SELECT 'capl_scripts',        COUNT(*) FROM capl_scripts
UNION ALL SELECT 'rag_chunks',          COUNT(*) FROM rag_chunks
UNION ALL SELECT 'rag_chunk_sync',      COUNT(*) FROM rag_chunk_sync
UNION ALL SELECT 'test_cases',          COUNT(*) FROM test_cases
UNION ALL SELECT 'python_test_scripts', COUNT(*) FROM python_test_scripts;
```

### View dataset versions loaded
```sql
SELECT id, name, version_number, status, created_at FROM dataset_versions ORDER BY version_number;
```

### View all ingested source files
```sql
SELECT relative_path, file_type, file_size_bytes, imported_at
FROM source_files
ORDER BY imported_at;
```

### View requirements
```sql
SELECT requirement_id, title FROM requirements LIMIT 20;
```

### View CAN messages
```sql
SELECT name, frame_id, dlc FROM can_messages;
```

### View CAN signals
```sql
SELECT name, start_bit, length, byte_order FROM can_signals LIMIT 20;
```

### View CAPL scripts (requirement → CAPL code pairs)
```sql
SELECT requirement_id, pattern_type FROM capl_scripts LIMIT 20;
```

### View test cases
```sql
SELECT test_case_id, title FROM test_cases LIMIT 20;
```

### View RAG chunks (text used for AI retrieval)
```sql
SELECT id, source_type, chunk_index, LEFT(chunk_text, 100) AS preview
FROM rag_chunks
LIMIT 10;
```

### Check Qdrant vector sync status
```sql
SELECT sync_status, COUNT(*) FROM rag_chunk_sync GROUP BY sync_status;
```

### View ingestion job history
```sql
SELECT id, status, started_at, completed_at FROM ingestion_jobs ORDER BY started_at DESC;
```

---

## Data Ingestion — Adding New Data

### Step 1 — Add new JSON files
Create a new folder `Data_V8/` (or next version) with your JSON files:
```
Data_V8/
├── CAPL_Data_01.json
└── Python_Script_data_01/
    └── pytest_data_REQ_01.json
```

### Step 2 — Run ingestion

```bash
# Incremental — only processes new/changed files (fastest)
python -m services.data_pipeline.ingest_postgres --mode incremental

# Full refresh — reprocesses all files from scratch
python -m services.data_pipeline.ingest_postgres --mode full_refresh

# Validation only — checks health, no writes
python -m services.data_pipeline.ingest_postgres --mode validation --skip-qdrant
```

### Step 3 — Sync embeddings to Qdrant (if skipped above)
```bash
python -m services.data_pipeline.rag_sync --batch-size 64 --collection rag_ecu
```

### Step 4 — Validate everything loaded correctly
```bash
python -m services.data_pipeline.validators
```

---

## Supported Input File Formats

| Format       | Folder Pattern                              | Description                        |
|--------------|---------------------------------------------|------------------------------------|
| `capl_json`  | `Data_V*/CAPL_Data_*.json`                  | CAN messages, signals, CAPL scripts|
| `pytest_json`| `Data_V*/Python_Script_data_*/pytest_data_REQ_*.json` | Requirements, test cases  |
| `rag_pdf`    | Future support (install `pypdf`)            | PDF documents                      |
| `rag_text`   | Future support                              | Plain text                         |

---

## GUI Access (pgAdmin or DBeaver)

Connect with:
- **Host:** `localhost`
- **Port:** `5432`
- **Database:** `ecu_testing`
- **Username:** `ecu_user`
- **Password:** `secret`

---

## Python — Quick Data Check (no extra install needed)

```bash
python -c "
import psycopg2
conn = psycopg2.connect('postgresql://ecu_user:secret@localhost:5432/ecu_testing')
cur = conn.cursor()
tables = ['dataset_versions','source_files','can_messages','requirements',
          'capl_scripts','rag_chunks','rag_chunk_sync','test_cases','python_test_scripts']
for t in tables:
    cur.execute(f'SELECT COUNT(*) FROM {t}')
    print(f'{t}: {cur.fetchone()[0]} rows')
conn.close()
"
```

---

## Current Data Snapshot (as of 2026-04-16)

| Table                | Rows | Description                        |
|----------------------|------|------------------------------------|
| `dataset_versions`   | 8    | Data_V1 to Data_V7 + app_runtime   |
| `source_files`       | 91   | All JSON files ingested             |
| `requirements`       | 84   | ECU test requirements               |
| `capl_scripts`       | 84   | CAPL code per requirement           |
| `can_messages`       | 45   | CAN frame definitions               |
| `can_signals`        | 127  | Signal definitions                  |
| `test_cases`         | 600  | Test case steps and expected results|
| `python_test_scripts`| 1290 | Pytest scripts                      |
| `rag_chunks`         | 736  | Text chunks for AI retrieval        |
| `rag_chunk_sync`     | 4    | Qdrant vector sync status           |
