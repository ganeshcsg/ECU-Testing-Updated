# SQL Database Implementation Plan

## Goal
Persist the `Data_V1`, `Data_V2`, `Data_V3`, and `Data_V4` datasets in a SQL database so the ECU project can:
- store structured CAPL/DBC metadata and Python test definitions
- query dataset versions, messages, signals, requirements, and test cases
- support both local SQL storage and production-grade SQL engines
- optionally link structured SQL data with semantic search later

## Current state
Your dataset folders contain:
- `Data_V*/CAPL_Data_*.json`: parsed DBC/CAN structure, messages, signals, and nodes
- `Data_V*/Python_Script_data_*/pytest_data_REQ_*.json`: requirements, test cases, Python test scripts, and CAN test setup

This plan converts those JSON sources into relational SQL tables.

## Recommended SQL design
Use SQLite for local development and PostgreSQL/MySQL for production.
The schema should preserve dataset versioning and make the raw JSON recoverable.

### Core tables
1. `dataset_versions`
   - `id` INTEGER PRIMARY KEY
   - `name` TEXT UNIQUE NOT NULL (`Data_V1`, `Data_V2`, ...)
   - `description` TEXT
   - `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP

2. `capl_documents`
   - `id` INTEGER PRIMARY KEY
   - `dataset_version_id` INTEGER NOT NULL REFERENCES dataset_versions(id)
   - `file_name` TEXT NOT NULL
   - `parsed_version` TEXT
   - `raw_json` TEXT
   - `file_modified_at` TIMESTAMP
   - `imported_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP

3. `can_messages`
   - `id` INTEGER PRIMARY KEY
   - `capl_document_id` INTEGER NOT NULL REFERENCES capl_documents(id)
   - `frame_id` INTEGER
   - `name` TEXT
   - `length` INTEGER
   - `senders` TEXT
   - `receivers` TEXT
   - `raw_payload` TEXT

4. `can_signals`
   - `id` INTEGER PRIMARY KEY
   - `message_id` INTEGER NOT NULL REFERENCES can_messages(id)
   - `name` TEXT
   - `start_bit` INTEGER
   - `length` INTEGER
   - `byte_order` TEXT
   - `is_signed` BOOLEAN
   - `scale` REAL
   - `offset` REAL
   - `minimum` REAL
   - `maximum` REAL
   - `unit` TEXT
   - `receivers` TEXT
   - `raw_payload` TEXT

5. `requirements`
   - `id` INTEGER PRIMARY KEY
   - `dataset_version_id` INTEGER NOT NULL REFERENCES dataset_versions(id)
   - `requirement_id` TEXT
   - `description` TEXT
   - `raw_json` TEXT
   - `file_name` TEXT
   - `file_modified_at` TIMESTAMP
   - `imported_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP

6. `test_cases`
   - `id` INTEGER PRIMARY KEY
   - `requirement_id` INTEGER NOT NULL REFERENCES requirements(id)
   - `test_case_id` TEXT
   - `title` TEXT
   - `precondition` TEXT
   - `steps` TEXT
   - `expected_result` TEXT
   - `python_test_script` TEXT
   - `raw_json` TEXT

7. `python_test_scripts` (optional)
   - `id` INTEGER PRIMARY KEY
   - `requirement_id` INTEGER NOT NULL REFERENCES requirements(id)
   - `setup_code` TEXT
   - `script_text` TEXT
   - `raw_json` TEXT

8. `import_log` (for tracking ingestion operations)
   - `id` INTEGER PRIMARY KEY
   - `dataset_version_id` INTEGER REFERENCES dataset_versions(id)
   - `operation_type` TEXT (full_refresh, incremental, validation)
   - `files_processed` INTEGER
   - `records_created` INTEGER
   - `records_updated` INTEGER
   - `errors` TEXT
   - `started_at` TIMESTAMP
   - `completed_at` TIMESTAMP

## Why this approach
- preserves dataset version lineage for `Data_V1`..`Data_V4`
- supports efficient SQL queries over messages, signals, and requirements
- keeps raw JSON payloads for audit and recovery
- provides a straightforward path to export or analyze structured ECU data

## Implementation steps

### 1. Choose your SQL engine
- local development: SQLite file like `ecu_data.db`
- production: PostgreSQL or MySQL

### 2. Create the SQL schema
Use a migration or schema creation script.
For SQLite, `sqlite3` is sufficient.
For PostgreSQL/MySQL, use SQLAlchemy or direct SQL.

### 3. Build an ingestion script
Write Python code that:
1. walks each `Data_V*` folder
2. inserts `dataset_versions` if missing
3. loads `CAPL_Data_*.json` into `capl_documents`
4. parses `messages` into `can_messages`
5. parses `signals` into `can_signals`
6. loads `pytest_data_REQ_*.json` into `requirements` and `test_cases`
7. optionally stores `python_test_scripts`

### 4. Store structured fields plus raw JSON
Keep parsed columns for important fields and also save the original JSON in `raw_json`.
This lets you query the database while preserving full original data.

### 5. Add ingestion metadata
Store `imported_at`, `dataset_version_id`, and source filename so refreshes are traceable.

### 6. Add refresh and validation
Support operations to:
- refresh a dataset version by deleting rows for that version and re-importing
- count rows by table and dataset version
- verify that every `Data_V*` folder is imported

### 7. Dynamic ingestion for new data
Design the ingestion workflow to automatically handle new data additions:

#### Auto-discovery of Data_Vx folders
- Scan the `Data/` directory for folders matching pattern `Data_V*` (e.g., `Data_V1`, `Data_V5`, `Data_V10`)
- Automatically create new `dataset_versions` entries for newly discovered folders
- Support any number of dataset versions without code changes

#### Incremental file processing
- Track `imported_at` timestamps and file modification times
- Only process files that are new or have been modified since last import
- Support adding new `CAPL_Data_*.json` or `pytest_data_REQ_*.json` files to existing `Data_Vx` folders
- Maintain import history and allow selective refreshes

#### File pattern flexibility
- Recognize any `CAPL_Data_*.json` files (not just `CAPL_Data_01.json`)
- Recognize any `pytest_data_REQ_*.json` files in any `Python_Script_data_*/` subfolders
- Support future file naming conventions without requiring code updates

#### Refresh strategies
- **Full refresh**: Clear all data for a dataset version and re-import everything
- **Incremental refresh**: Only process new/changed files
- **Selective refresh**: Refresh specific files or folders
- **Validation refresh**: Check for missing files and re-import them

#### Workflow integration
- Add ingestion as a scheduled job or manual trigger in the ECU app
- Provide CLI commands: `ingest_data`, `refresh_version Data_V5`, `validate_imports`
- Log import operations with timestamps and file counts
- Handle import failures gracefully with rollback options

## Example SQL table definitions (SQLite)
```sql
CREATE TABLE dataset_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  description TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE capl_documents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dataset_version_id INTEGER NOT NULL REFERENCES dataset_versions(id),
  file_name TEXT NOT NULL,
  parsed_version TEXT,
  raw_json TEXT,
  file_modified_at TEXT,
  imported_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE can_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  capl_document_id INTEGER NOT NULL REFERENCES capl_documents(id),
  frame_id INTEGER,
  name TEXT,
  length INTEGER,
  senders TEXT,
  receivers TEXT,
  raw_payload TEXT
);

CREATE TABLE can_signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id INTEGER NOT NULL REFERENCES can_messages(id),
  name TEXT,
  start_bit INTEGER,
  length INTEGER,
  byte_order TEXT,
  is_signed INTEGER,
  scale REAL,
  offset REAL,
  minimum REAL,
  maximum REAL,
  unit TEXT,
  receivers TEXT,
  raw_payload TEXT
);

CREATE TABLE requirements (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dataset_version_id INTEGER NOT NULL REFERENCES dataset_versions(id),
  requirement_id TEXT,
  description TEXT,
  raw_json TEXT,
  file_name TEXT,
  file_modified_at TEXT,
  imported_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE test_cases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  requirement_id INTEGER NOT NULL REFERENCES requirements(id),
  test_case_id TEXT,
  title TEXT,
  precondition TEXT,
  steps TEXT,
  expected_result TEXT,
  python_test_script TEXT,
  raw_json TEXT
);

CREATE TABLE python_test_scripts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  requirement_id INTEGER NOT NULL REFERENCES requirements(id),
  setup_code TEXT,
  script_text TEXT,
  raw_json TEXT
);

CREATE TABLE import_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dataset_version_id INTEGER REFERENCES dataset_versions(id),
  operation_type TEXT,
  files_processed INTEGER,
  records_created INTEGER,
  records_updated INTEGER,
  errors TEXT,
  started_at TEXT,
  completed_at TEXT
);
```

## SQL ingestion flow
1. insert or update `dataset_versions` for each version folder
2. insert `CAPL_Data_*.json` into `capl_documents`
3. insert `messages` into `can_messages`
4. insert `signals` into `can_signals`
5. insert requirement records into `requirements`
6. insert `test_cases` and optional `python_test_scripts`

## Dynamic ingestion workflow
The ingestion system should automatically handle new data additions:

### Auto-discovery process
```python
def discover_dataset_versions(data_dir="./Data"):
    """Find all Data_Vx folders and return sorted list"""
    import glob
    pattern = os.path.join(data_dir, "Data_V*")
    versions = []
    for folder in glob.glob(pattern):
        if os.path.isdir(folder):
            version_name = os.path.basename(folder)
            versions.append(version_name)
    return sorted(versions)  # ['Data_V1', 'Data_V2', 'Data_V3', 'Data_V4', ...]
```

### Incremental processing logic
- Check `file_modified_at` vs actual file modification time
- Only process files newer than last import
- Update existing records instead of inserting duplicates
- Log operations in `import_log` table

### File pattern matching
- `CAPL_Data_*.json`: any CAPL data file in Data_Vx/
- `Python_Script_data_*/pytest_data_REQ_*.json`: any requirement file in any Python_Script_data_xx/ subfolder
- Support future naming patterns without code changes

### Refresh operations
- **Full refresh**: Delete all records for a dataset version, then re-import
- **Incremental refresh**: Process only new/changed files
- **Validation refresh**: Check for missing files and import them

## Example queries
- Requirements in `Data_V2`:
  `SELECT r.* FROM requirements r JOIN dataset_versions d ON r.dataset_version_id = d.id WHERE d.name='Data_V2';`
- Messages sent by `BCM`:
  `SELECT * FROM can_messages WHERE senders LIKE '%BCM%';`
- Test cases for `REQ_01`:
  `SELECT * FROM test_cases WHERE requirement_id = (SELECT id FROM requirements WHERE requirement_id='REQ_01');`

## Optional extension: SQL + semantic search
If you want semantic search later, store structured data in SQL and keep a separate vector index in Qdrant.
- add SQL record IDs to Qdrant payloads
- use SQL as the authoritative source of truth

## Validation and maintenance
- confirm row counts for each dataset version
- verify every JSON file is imported
- support dataset refresh by deleting rows for a version and reloading
- keep raw JSON for auditability

## Recommended next steps
1. choose SQLite or PostgreSQL for your deployment
2. implement the SQL schema script (including `import_log` table)
3. write the ingestion loader for the `Data_V*` folders with auto-discovery
4. implement incremental processing logic using file modification times
5. add CLI commands: `ingest_data`, `refresh_version Data_V5`, `validate_imports`
6. load `CAPL_Data_*.json` and `pytest_data_REQ_*.json` into SQL
7. add dashboard/report queries for dataset versions and import history

## Key considerations for new data handling
- **Auto-discovery**: The system scans for new `Data_Vx` folders automatically
- **Incremental updates**: Only processes files that have changed since last import
- **Flexible file patterns**: Supports any `CAPL_Data_*.json` or `pytest_data_REQ_*.json` files
- **Import logging**: Tracks all ingestion operations in `import_log` table
- **Refresh strategies**: Full, incremental, and validation refresh modes
- **Future-proof**: Can handle new dataset versions and file naming conventions without code changes

---

This plan is built for storing `Data_V1`..`Data_V4` in SQL so your ECU project can query and manage the datasets directly.