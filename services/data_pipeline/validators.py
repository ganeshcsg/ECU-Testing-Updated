"""
Validation and health-check queries for the ingestion database layer.

Usage:
    python -m services.data_pipeline.validators [--verbose]

Reports:
- Row counts per table
- Files registered vs files on disk
- Chunks pending Qdrant sync
- Sync failure details
- Missing or partially imported records
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BASE_DIR = Path(__file__).resolve().parent.parent.parent


# --- Row count summary --------------------------------------------------------

_COUNT_QUERIES: List[Tuple[str, str]] = [
    ("dataset_versions",     "SELECT COUNT(*) FROM dataset_versions"),
    ("source_files",         "SELECT COUNT(*) FROM source_files"),
    ("ingestion_jobs",       "SELECT COUNT(*) FROM ingestion_jobs"),
    ("ingestion_job_items",  "SELECT COUNT(*) FROM ingestion_job_items"),
    ("capl_documents",       "SELECT COUNT(*) FROM capl_documents"),
    ("can_nodes",            "SELECT COUNT(*) FROM can_nodes"),
    ("can_messages",         "SELECT COUNT(*) FROM can_messages"),
    ("can_signals",          "SELECT COUNT(*) FROM can_signals"),
    ("capl_scripts",         "SELECT COUNT(*) FROM capl_scripts"),
    ("requirements",         "SELECT COUNT(*) FROM requirements"),
    ("requirement_can_msgs", "SELECT COUNT(*) FROM requirement_can_messages"),
    ("test_cases",           "SELECT COUNT(*) FROM test_cases"),
    ("python_test_scripts",  "SELECT COUNT(*) FROM python_test_scripts"),
    ("rag_documents",        "SELECT COUNT(*) FROM rag_documents"),
    ("rag_chunks",           "SELECT COUNT(*) FROM rag_chunks"),
    ("rag_chunk_sync",       "SELECT COUNT(*) FROM rag_chunk_sync"),
]


def count_summary(conn) -> Dict[str, int]:
    cur = conn.cursor()
    result: Dict[str, int] = {}
    for label, sql in _COUNT_QUERIES:
        try:
            cur.execute(sql)
            result[label] = cur.fetchone()[0]
        except Exception as exc:
            result[label] = -1
            log.warning("Count failed for %s: %s", label, exc)
    cur.close()
    return result


# --- File-vs-DB comparison ----------------------------------------------------

def file_registration_check(conn, base_dir: Path = BASE_DIR) -> Dict[str, list]:
    """
    Compare files on disk with source_files in DB.
    Returns {registered: [...], missing_in_db: [...], extra_in_db: [...]}.
    """
    import glob as _glob
    import re

    disk_files: Dict[str, str] = {}
    pattern = re.compile(r"^Data_V(\d+)$", re.IGNORECASE)
    for d in sorted(base_dir.iterdir()):
        if d.is_dir() and pattern.match(d.name):
            for p in sorted(d.glob("CAPL_Data_*.json")):
                rel = str(p.relative_to(base_dir)).replace("\\", "/")
                disk_files[rel] = "capl_json"
            for p in sorted(d.rglob("pytest_data_REQ_*.json")):
                rel = str(p.relative_to(base_dir)).replace("\\", "/")
                disk_files[rel] = "pytest_json"

    cur = conn.cursor()
    cur.execute("SELECT relative_path, file_type FROM source_files")
    db_files: Dict[str, str] = {
        row[0].replace("\\", "/"): row[1] for row in cur.fetchall()
    }
    cur.close()

    registered     = [p for p in disk_files if p in db_files]
    missing_in_db  = [p for p in disk_files if p not in db_files]
    extra_in_db    = [p for p in db_files if p not in disk_files]

    return {
        "registered":    registered,
        "missing_in_db": missing_in_db,
        "extra_in_db":   extra_in_db,
    }


# --- Qdrant sync health -------------------------------------------------------

def sync_health(conn) -> Dict[str, int]:
    """
    Return counts of rag_chunk_sync rows by status.
    Also returns how many rag_chunks have NO sync row (truly pending).
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT sync_status, COUNT(*) FROM rag_chunk_sync GROUP BY sync_status ORDER BY sync_status
        """
    )
    by_status = {row[0]: row[1] for row in cur.fetchall()}

    cur.execute(
        """
        SELECT COUNT(*) FROM rag_chunks rc
        WHERE NOT EXISTS (SELECT 1 FROM rag_chunk_sync rcs WHERE rcs.rag_chunk_id = rc.id)
        """
    )
    by_status["no_sync_row"] = cur.fetchone()[0]
    cur.close()
    return by_status


def sync_failures(conn, limit: int = 20) -> List[dict]:
    """Return up to *limit* failed sync entries with context."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT rcs.id, rcs.rag_chunk_id, rcs.error_message,
               rd.source_type, rd.document_title,
               dv.name AS dataset_version
        FROM rag_chunk_sync rcs
        JOIN rag_chunks rc ON rc.id = rcs.rag_chunk_id
        JOIN rag_documents rd ON rd.id = rc.rag_document_id
        JOIN dataset_versions dv ON dv.id = rd.dataset_version_id
        WHERE rcs.sync_status = 'failed'
        ORDER BY rcs.created_at DESC
        LIMIT %s
        """,
        (limit,),
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    cur.close()
    return rows


# --- Dataset-level completeness -----------------------------------------------

def dataset_completeness(conn) -> List[dict]:
    """
    Per dataset_version: file counts, requirement counts, capl_script counts,
    rag_document counts, chunk counts.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            dv.name,
            COUNT(DISTINCT sf.id)   AS source_files,
            COUNT(DISTINCT cd.id)   AS capl_documents,
            COUNT(DISTINCT cs.id)   AS capl_scripts,
            COUNT(DISTINCT r.id)    AS requirements,
            COUNT(DISTINCT tc.id)   AS test_cases,
            COUNT(DISTINCT rd.id)   AS rag_documents,
            COUNT(DISTINCT rc.id)   AS rag_chunks
        FROM dataset_versions dv
        LEFT JOIN source_files sf     ON sf.dataset_version_id = dv.id
        LEFT JOIN capl_documents cd   ON cd.dataset_version_id = dv.id
        LEFT JOIN capl_scripts cs     ON cs.capl_document_id = cd.id
        LEFT JOIN requirements r      ON r.dataset_version_id = dv.id
        LEFT JOIN test_cases tc       ON tc.requirement_id = r.id
        LEFT JOIN rag_documents rd    ON rd.dataset_version_id = dv.id
        LEFT JOIN rag_chunks rc       ON rc.rag_document_id = rd.id
        GROUP BY dv.name
        ORDER BY dv.name
        """
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    cur.close()
    return rows


# --- Latest ingestion jobs ----------------------------------------------------

def recent_jobs(conn, limit: int = 5) -> List[dict]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, job_type, status, triggered_by,
               started_at, completed_at,
               files_discovered, files_processed,
               records_created, records_skipped, error_summary
        FROM ingestion_jobs
        ORDER BY created_at DESC
        LIMIT %s
        """,
        (limit,),
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    cur.close()
    return rows


# --- Full validation report ---------------------------------------------------

def run_validation(conn, base_dir: Path = BASE_DIR, verbose: bool = False) -> bool:
    """
    Run all checks and print a report. Returns True if everything looks healthy.
    """
    ok = True
    sep = "-" * 60

    print(sep)
    print("TABLE ROW COUNTS")
    print(sep)
    counts = count_summary(conn)
    for label, count in counts.items():
        flag = " !" if count == 0 else ""
        print(f"  {label:<24} {count:>8}{flag}")

    print()
    print(sep)
    print("DATASET COMPLETENESS")
    print(sep)
    for row in dataset_completeness(conn):
        print(f"  {row['name']}: "
              f"files={row['source_files']} "
              f"capl_scripts={row['capl_scripts']} "
              f"requirements={row['requirements']} "
              f"test_cases={row['test_cases']} "
              f"rag_chunks={row['rag_chunks']}")

    print()
    print(sep)
    print("FILE REGISTRATION CHECK")
    print(sep)
    reg = file_registration_check(conn, base_dir)
    print(f"  Registered:    {len(reg['registered'])}")
    print(f"  Missing in DB: {len(reg['missing_in_db'])}")
    print(f"  Extra in DB:   {len(reg['extra_in_db'])}")
    if reg["missing_in_db"]:
        ok = False
        print("  MISSING:")
        for p in reg["missing_in_db"]:
            print(f"    {p}")
    if verbose and reg["extra_in_db"]:
        print("  EXTRA (in DB but not on disk):")
        for p in reg["extra_in_db"]:
            print(f"    {p}")

    print()
    print(sep)
    print("QDRANT SYNC HEALTH")
    print(sep)
    sh = sync_health(conn)
    for status, count in sorted(sh.items()):
        flag = " !" if status in ("failed", "stale", "no_sync_row") and count > 0 else ""
        print(f"  {status:<16} {count:>8}{flag}")
        if status in ("failed", "stale", "no_sync_row") and count > 0:
            ok = False

    failures = sync_failures(conn, limit=10)
    if failures:
        print()
        print("  Recent sync failures:")
        for f in failures:
            print(f"    chunk_id={f['rag_chunk_id']} [{f['dataset_version']}:{f['source_type']}] {f['error_message']}")

    print()
    print(sep)
    print("RECENT INGESTION JOBS")
    print(sep)
    for job in recent_jobs(conn):
        duration = ""
        if job["started_at"] and job["completed_at"]:
            delta = job["completed_at"] - job["started_at"]
            duration = f" ({int(delta.total_seconds())}s)"
        print(f"  [{job['id']}] {job['job_type']} -> {job['status']}{duration} "
              f"files={job['files_processed']}/{job['files_discovered']} "
              f"created={job['records_created']}")
        if job["error_summary"] and verbose:
            print(f"    ERRORS: {job['error_summary'][:200]}")

    print()
    print("RESULT:", "OK" if ok else "ISSUES FOUND")
    return ok


# --- CLI ----------------------------------------------------------------------

def _cli() -> None:
    parser = argparse.ArgumentParser(description="ECU Testing AI — database validation")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--base-dir", default=None)
    args = parser.parse_args()
    base = Path(args.base_dir) if args.base_dir else BASE_DIR

    from services.data_pipeline.db import get_connection
    conn = get_connection()
    try:
        ok = run_validation(conn, base_dir=base, verbose=args.verbose)
    finally:
        conn.close()

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    _cli()
