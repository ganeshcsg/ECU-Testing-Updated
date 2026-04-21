"""
PostgreSQL bridge for the Streamlit app.

Responsibilities
----------------
1. One-stop init: ensure schema, bootstrap dataset version, run structural
   ingestion (Data_V1–V7) on first boot.
2. Provide the live pg_conn used by ExtendedRAGVectorStore to record every
   Qdrant point in rag_documents / rag_chunks / rag_chunk_sync.
3. Persist generated artifacts (CAPL + test cases) to generated_artifacts.

All failures are caught and logged; the app continues without the DB.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional, Tuple

log = logging.getLogger(__name__)

try:
    import psycopg2
    import psycopg2.extras
    _PSYCOPG2 = True
except ImportError:
    _PSYCOPG2 = False  # type: ignore


# ── Connection helpers ────────────────────────────────────────────────────────

def is_db_configured() -> bool:
    """True when DATABASE_URL is set AND psycopg2 is installed."""
    return _PSYCOPG2 and bool(os.environ.get("DATABASE_URL", "").strip())


def get_connection(autocommit: bool = False):
    """Open a fresh psycopg2 connection from DATABASE_URL."""
    if not _PSYCOPG2:
        raise ImportError("psycopg2-binary not installed: pip install psycopg2-binary")
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    url = re.sub(r"^postgresql\+\w+://", "postgresql://", url)
    conn = psycopg2.connect(url)
    conn.autocommit = autocommit
    psycopg2.extras.register_json(conn, globally=False, loads=json.loads)
    return conn


# ── Schema bootstrap ──────────────────────────────────────────────────────────

def ensure_schema() -> None:
    """
    Apply pending Alembic migrations.
    Falls back to running schema_postgres.sql directly if Alembic is unavailable.
    """
    alembic_ini = Path(__file__).resolve().parent.parent.parent / "database" / "alembic.ini"
    try:
        from alembic.config import Config
        from alembic import command
        if not alembic_ini.exists():
            raise FileNotFoundError(alembic_ini)
        cfg = Config(str(alembic_ini))
        command.upgrade(cfg, "head")
        log.info("Alembic migrations applied")
        return
    except ImportError:
        log.warning("alembic not installed; falling back to direct SQL schema")
    except Exception as exc:
        log.warning("Alembic migration failed (%s); falling back to direct SQL", exc)

    # Fallback: run the SQL file statement-by-statement
    schema_sql = Path(__file__).resolve().parent.parent.parent / "database" / "schema_postgres.sql"
    if not schema_sql.exists():
        raise FileNotFoundError(f"schema_postgres.sql not found at {schema_sql}")
    conn = get_connection(autocommit=False)
    cur = conn.cursor()
    for stmt in schema_sql.read_text(encoding="utf-8").split(";"):
        stmt = stmt.strip()
        if not stmt or stmt.startswith("--"):
            continue
        try:
            cur.execute(stmt)
        except Exception:
            conn.rollback()
    conn.commit()
    cur.close()
    conn.close()
    log.info("Schema created via schema_postgres.sql")


# ── Dataset version for app-loaded Qdrant chunks ──────────────────────────────

def ensure_app_runtime_version(conn) -> int:
    """
    Ensure a special 'app_runtime' dataset_versions row exists.
    This is the owner for rag_documents created by the live Streamlit app
    (as opposed to the offline ingestion pipeline).
    Returns the row id.
    """
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO dataset_versions (name, version_number, source_path, status)
        VALUES ('app_runtime', 0, 'runtime', 'active')
        ON CONFLICT (name) DO NOTHING
        RETURNING id
        """
    )
    row = cur.fetchone()
    if not row:
        cur.execute("SELECT id FROM dataset_versions WHERE name = 'app_runtime'")
        row = cur.fetchone()
    cur.close()
    if not row:
        raise RuntimeError("Failed to create app_runtime dataset version")
    return row[0]


# ── Structural ingestion (Data_V1 → Data_V7) ─────────────────────────────────

def _needs_structural_sync(conn) -> bool:
    """True when source_files table is empty (first-time run)."""
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM source_files")
    count = cur.fetchone()[0]
    cur.close()
    return count == 0


def run_structural_sync(base_dir: Path) -> dict:
    """
    Run the full structural ingestion pipeline (skip Qdrant embedding sync).
    Creates its own DB connection internally.
    """
    from services.data_pipeline.ingest_postgres import run_ingestion
    return run_ingestion(
        base_dir=base_dir,
        job_type="full_refresh",
        triggered_by="app_startup",
        skip_qdrant=True,
    )


# ── Main app-startup entry point ──────────────────────────────────────────────

def init_for_app(base_dir: Path) -> Tuple[Optional[Any], Optional[int]]:
    """
    One-shot setup called at Streamlit startup:
      1. Ensure schema
      2. Open an autocommit connection (used by RAGVectorStore for live writes)
      3. Ensure 'app_runtime' dataset version
      4. Run structural ingestion on first boot only

    Returns (conn, dv_id) — both None when DB is not configured or init fails.
    """
    if not is_db_configured():
        return None, None
    try:
        ensure_schema()
        conn = get_connection(autocommit=True)
        dv_id = ensure_app_runtime_version(conn)

        if _needs_structural_sync(conn):
            log.info("First-time DB sync: ingesting Data_V* from %s", base_dir)
            try:
                stats = run_structural_sync(base_dir)
                log.info(
                    "Structural sync done: files=%d records=%d errors=%d",
                    stats.get("files_processed", 0),
                    stats.get("records_created", 0),
                    len(stats.get("errors", [])),
                )
            except Exception as exc:
                log.warning("Structural sync failed (non-fatal): %s", exc)
        else:
            log.info("DB already populated; skipping structural sync")

        return conn, dv_id
    except Exception as exc:
        log.error("pg_bridge.init_for_app failed: %s", exc)
        return None, None


# ── Artifact recording ────────────────────────────────────────────────────────

def record_artifact(
    conn,
    requirement: str,
    dbc_summary: str,
    test_cases: dict,
    capl_code: str,
    llm_model: Optional[str] = None,
    generation_seconds: Optional[float] = None,
) -> Optional[int]:
    """
    Persist one generation run to generated_artifacts.
    Returns the new row id, or None on failure.
    """
    if not conn:
        return None
    try:
        cur = conn.cursor()

        # Best-effort: try to find the matching requirement row by description text
        req_db_id: Optional[int] = None
        desc_prefix = requirement[:200]
        cur.execute(
            "SELECT id FROM requirements WHERE description LIKE %s LIMIT 1",
            (desc_prefix.replace("%", "%%") + "%",),
        )
        row = cur.fetchone()
        if row:
            req_db_id = row[0]

        cur.execute(
            """
            INSERT INTO generated_artifacts
                (requirement_id, generated_test_cases, generated_capl_code,
                 llm_model, status, generation_time_seconds, created_by)
            VALUES (%s, %s, %s, %s, 'success', %s, 'streamlit_app')
            RETURNING id
            """,
            (
                req_db_id,
                json.dumps(test_cases),
                capl_code,
                llm_model,
                generation_seconds,
            ),
        )
        artifact_row = cur.fetchone()
        cur.close()
        return artifact_row[0] if artifact_row else None
    except Exception as exc:
        log.warning("record_artifact failed (non-fatal): %s", exc)
        return None
