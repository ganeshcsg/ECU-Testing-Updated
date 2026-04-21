"""
Ingestion pipeline: Data_V1 … Data_V7 → PostgreSQL.

Entry point:
    python -m services.data_pipeline.ingest_postgres [--mode full_refresh|incremental|validation]

Steps
-----
1. Discover Data_V* folders → dataset_versions
2. Register source files (SHA-256 dedup) → source_files
3. Parse CAPL JSON → capl_documents, can_nodes, can_messages, can_signals, capl_scripts
4. Parse pytest JSON → requirements, requirement_can_messages, test_cases, python_test_scripts
5. Build RAG document text → rag_documents
6. Chunk documents → rag_chunks
7. Sync embeddings to Qdrant → rag_chunk_sync
8. Audit → ingestion_jobs, ingestion_job_items
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psycopg2.extras

from services.data_pipeline.db import get_connection, managed_connection

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ─── Constants ────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent.parent.parent  # repo root

# Chunking: ~4 chars per token → 600 tok target
CHUNK_CHARS = 2400
CHUNK_OVERLAP = 300

_CAPL_PATTERN_RE = {
    "cyclic_timer":      re.compile(r"\bon\s+timer\b|\bmsTimer\b", re.IGNORECASE),
    "reactive_message":  re.compile(r"\bon\s+message\b", re.IGNORECASE),
    "reactive_key":      re.compile(r"\bon\s+key\b", re.IGNORECASE),
    "on_start":          re.compile(r"\bon\s+start\b", re.IGNORECASE),
}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _detect_capl_pattern(script: str) -> str:
    for name, rx in _CAPL_PATTERN_RE.items():
        if rx.search(script):
            return name
    return "variables"


def _chunk_text(text: str) -> List[str]:
    """Split *text* into overlapping chunks. Short texts (≤ CHUNK_CHARS) are returned as-is."""
    if len(text) <= CHUNK_CHARS:
        return [text]
    chunks: List[str] = []
    start = 0
    while start < len(text):
        end = min(start + CHUNK_CHARS, len(text))
        # Try to break at a newline for clean chunk boundaries
        if end < len(text):
            nl = text.rfind("\n", start, end)
            if nl > start:
                end = nl + 1
        chunks.append(text[start:end].strip())
        start = end - CHUNK_OVERLAP
    return [c for c in chunks if c]


def _req_doc_text(req_id: str, description: str, can_messages: list, python_setup: Optional[str]) -> str:
    lines = [f"Requirement ID: {req_id}", f"Description: {description}"]
    if can_messages:
        lines.append("Related CAN Messages:")
        for m in can_messages:
            lines.append(
                f"  - Node: {m.get('node', '')}, "
                f"ID: {m.get('arbitration_id', '')}, "
                f"Signal: {m.get('signal', '')}, "
                f"Bit: {m.get('bit', '')}"
            )
    if python_setup:
        lines.append("Python Test Setup:")
        lines.append(python_setup)
    return "\n".join(lines)


def _msg_doc_text(msg: dict, dataset_version: str) -> str:
    senders = ", ".join(msg.get("senders", [])) or "unknown"
    lines = [
        f"Message: {msg['name']}",
        f"Frame ID: {msg['frame_id']} (0x{msg['frame_id']:X})",
        f"Dataset: {dataset_version}",
        f"Sender: {senders}",
        "Signals:",
    ]
    for sig in msg.get("signals", []):
        lines.append(
            f"  - {sig['name']} start={sig.get('start', '?')} "
            f"len={sig.get('length', '?')} {sig.get('byte_order', '')} "
            f"unit={sig.get('unit', '') or 'none'}"
        )
    return "\n".join(lines)


def _capl_doc_text(req_id: str, req_text: str, capl_script: str) -> str:
    return f"Requirement: {req_id}\nDescription: {req_text}\nCAPL Script:\n{capl_script}"


def _tc_doc_text(req_id: str, tc: dict) -> str:
    steps = tc.get("steps", [])
    steps_str = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(steps)) if isinstance(steps, list) else str(steps)
    return (
        f"Test Case: {tc.get('test_case_id', '')}\n"
        f"Title: {tc.get('title', '')}\n"
        f"Requirement: {req_id}\n"
        f"Precondition: {tc.get('precondition', '')}\n"
        f"Steps:\n{steps_str}\n"
        f"Expected Result: {tc.get('expected_result', '')}"
    )


# ─── Step 1: Discover dataset versions ────────────────────────────────────────

def _discover_dataset_versions(cur, base_dir: Path) -> List[Tuple[str, int]]:
    """Insert/update dataset_versions rows. Returns list of (dv_name, dv_id)."""
    pattern = re.compile(r"^Data_V(\d+)$", re.IGNORECASE)
    results: List[Tuple[str, int]] = []
    folders = sorted(
        (d for d in base_dir.iterdir() if d.is_dir() and pattern.match(d.name)),
        key=lambda d: int(pattern.match(d.name).group(1)),
    )
    for folder in folders:
        m = pattern.match(folder.name)
        version_num = int(m.group(1))
        cur.execute(
            """
            INSERT INTO dataset_versions (name, version_number, source_path, status)
            VALUES (%s, %s, %s, 'active')
            ON CONFLICT (name) DO UPDATE
                SET version_number = EXCLUDED.version_number,
                    source_path    = EXCLUDED.source_path,
                    updated_at     = CURRENT_TIMESTAMP
            RETURNING id
            """,
            (folder.name, version_num, str(folder)),
        )
        dv_id = cur.fetchone()[0]
        results.append((folder.name, dv_id))
        log.info("dataset_version: %s → id=%d", folder.name, dv_id)
    return results


# ─── Step 2: Register source files ────────────────────────────────────────────

def _register_source_file(
    cur, job_id: int, dv_id: int, dv_name: str, file_path: Path, base_dir: Path, file_type: str
) -> Optional[Tuple[int, dict, bool]]:
    """
    Insert source_files row for *file_path*.
    Returns (sf_id, raw_json, is_new) or None if file cannot be read.
    is_new=False means the exact same hash already exists (skip re-processing).
    """
    try:
        raw_bytes = file_path.read_bytes()
        raw_json = json.loads(raw_bytes)
    except Exception as exc:
        log.warning("Cannot read %s: %s", file_path, exc)
        cur.execute(
            """
            INSERT INTO ingestion_job_items (ingestion_job_id, relative_path, status, detail)
            VALUES (%s, %s, 'failed', %s)
            """,
            (job_id, str(file_path.relative_to(base_dir)), str(exc)),
        )
        return None

    file_hash = _sha256(raw_bytes.decode("utf-8", errors="replace"))
    rel_path = str(file_path.relative_to(base_dir))
    stat = file_path.stat()
    mod_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)

    cur.execute(
        """
        INSERT INTO source_files
            (dataset_version_id, file_type, relative_path, file_name, file_hash,
             file_size_bytes, file_modified_at, raw_json)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (dataset_version_id, relative_path, file_hash) DO NOTHING
        RETURNING id
        """,
        (
            dv_id, file_type, rel_path, file_path.name, file_hash,
            stat.st_size, mod_at, json.dumps(raw_json),
        ),
    )
    row = cur.fetchone()
    if row:
        sf_id = row[0]
        is_new = True
    else:
        # Already exists with same hash; fetch existing id
        cur.execute(
            "SELECT id FROM source_files WHERE dataset_version_id=%s AND relative_path=%s AND file_hash=%s",
            (dv_id, rel_path, file_hash),
        )
        existing = cur.fetchone()
        sf_id = existing[0] if existing else None
        is_new = False

    return (sf_id, raw_json, is_new)


# ─── Step 3a: Parse CAPL JSON ─────────────────────────────────────────────────

def _process_capl_file(
    cur, job_id: int, sf_id: int, dv_id: int, dv_name: str, raw_json: dict, rel_path: str
) -> dict:
    stats = {"created": 0, "skipped": 0}

    dbc_key = next((k for k in raw_json if "DBC" in k), None)
    if not dbc_key:
        log.warning("%s: no DBC key found, skipping", rel_path)
        return stats

    dbc = raw_json[dbc_key]
    parsed_version = dbc.get("version", "")

    # capl_documents
    cur.execute(
        """
        INSERT INTO capl_documents (dataset_version_id, source_file_id, parsed_version, dbc_root_key, raw_json)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (dataset_version_id, source_file_id) DO NOTHING
        RETURNING id
        """,
        (dv_id, sf_id, parsed_version, dbc_key, json.dumps(dbc)),
    )
    row = cur.fetchone()
    if row:
        capl_doc_id = row[0]
        stats["created"] += 1
    else:
        cur.execute(
            "SELECT id FROM capl_documents WHERE dataset_version_id=%s AND source_file_id=%s",
            (dv_id, sf_id),
        )
        capl_doc_id = cur.fetchone()[0]
        stats["skipped"] += 1

    # can_nodes
    for node_name in dbc.get("nodes", []):
        cur.execute(
            """
            INSERT INTO can_nodes (capl_document_id, node_name)
            VALUES (%s, %s)
            ON CONFLICT (capl_document_id, node_name) DO NOTHING
            """,
            (capl_doc_id, str(node_name)),
        )
        stats["created"] += cur.rowcount

    # can_messages + can_signals
    for msg in dbc.get("messages", []):
        frame_id = msg.get("frame_id", 0)
        msg_name = msg.get("name", "")
        senders = msg.get("senders", [])
        # Collect receiver list from all signals
        all_receivers: List[str] = []
        for sig in msg.get("signals", []):
            all_receivers.extend(sig.get("receivers", []))
        unique_receivers = list(dict.fromkeys(all_receivers))

        cur.execute(
            """
            INSERT INTO can_messages
                (capl_document_id, frame_id, frame_id_hex, name, dlc, message_length,
                 senders, receivers, raw_payload)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (capl_document_id, frame_id, name) DO NOTHING
            RETURNING id
            """,
            (
                capl_doc_id, frame_id, f"0x{frame_id:X}", msg_name,
                msg.get("length"), msg.get("length"),
                senders, unique_receivers, json.dumps(msg),
            ),
        )
        msg_row = cur.fetchone()
        if msg_row:
            can_msg_id = msg_row[0]
            stats["created"] += 1
        else:
            cur.execute(
                "SELECT id FROM can_messages WHERE capl_document_id=%s AND frame_id=%s AND name=%s",
                (capl_doc_id, frame_id, msg_name),
            )
            can_msg_id = cur.fetchone()[0]

        for sig in msg.get("signals", []):
            start_bit = sig.get("start")
            cur.execute(
                """
                INSERT INTO can_signals
                    (can_message_id, name, start_bit, signal_length, byte_order,
                     is_signed, scale, signal_offset, minimum, maximum, unit, receivers, raw_payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (can_message_id, name, start_bit) DO NOTHING
                """,
                (
                    can_msg_id,
                    sig.get("name", ""),
                    start_bit,
                    sig.get("length"),
                    sig.get("byte_order", ""),
                    sig.get("is_signed"),
                    sig.get("scale"),
                    sig.get("offset"),
                    sig.get("minimum"),
                    sig.get("maximum"),
                    sig.get("unit", ""),
                    sig.get("receivers", []),
                    json.dumps(sig),
                ),
            )
            stats["created"] += cur.rowcount

    # capl_scripts — requirement-CAPL training pairs
    for req in raw_json.get("requirements", []):
        req_id = req.get("requirement_id", "")
        req_text = req.get("requirement_text", "")
        capl_script = req.get("capl_script", "")
        pattern = _detect_capl_pattern(capl_script)
        cur.execute(
            """
            INSERT INTO capl_scripts (capl_document_id, requirement_id, requirement_text, capl_script, capl_pattern)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (capl_document_id, requirement_id) DO NOTHING
            """,
            (capl_doc_id, req_id, req_text, capl_script, pattern),
        )
        stats["created"] += cur.rowcount

    cur.execute(
        """
        INSERT INTO ingestion_job_items (ingestion_job_id, source_file_id, relative_path, status, detail)
        VALUES (%s, %s, %s, 'processed', %s)
        """,
        (job_id, sf_id, rel_path, f"capl_doc_id={capl_doc_id}"),
    )
    log.info("  CAPL %s → capl_doc_id=%d created=%d skipped=%d", rel_path, capl_doc_id, stats["created"], stats["skipped"])
    return stats


# ─── Step 3b: Parse pytest JSON ───────────────────────────────────────────────

def _process_pytest_file(
    cur, job_id: int, sf_id: int, dv_id: int, dv_name: str, raw_json: dict, rel_path: str
) -> dict:
    stats = {"created": 0, "skipped": 0}

    req_obj = raw_json.get("requirement", {})
    req_id = req_obj.get("requirement_id", "")
    description = req_obj.get("description", "")
    python_setup = req_obj.get("python_test_setup", "")

    if not req_id or not description:
        log.warning("%s: missing requirement_id or description, skipping", rel_path)
        return stats

    cur.execute(
        """
        INSERT INTO requirements
            (dataset_version_id, source_file_id, requirement_id, description, python_test_setup, raw_json)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (dataset_version_id, requirement_id) DO UPDATE
            SET description       = EXCLUDED.description,
                python_test_setup = EXCLUDED.python_test_setup,
                raw_json          = EXCLUDED.raw_json,
                updated_at        = CURRENT_TIMESTAMP
        RETURNING id
        """,
        (dv_id, sf_id, req_id, description, python_setup or None, json.dumps(req_obj)),
    )
    req_row = cur.fetchone()
    req_db_id = req_row[0]
    stats["created"] += 1

    # requirement_can_messages
    for cm in req_obj.get("can_messages", []):
        cur.execute(
            """
            INSERT INTO requirement_can_messages
                (requirement_id, node_name, arbitration_id, signal_name, bit_position, raw_payload)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                req_db_id,
                cm.get("node", ""),
                cm.get("arbitration_id", ""),
                cm.get("signal", ""),
                cm.get("bit"),
                json.dumps(cm),
            ),
        )
        stats["created"] += 1

    # python_test_scripts — setup block
    if python_setup:
        cur.execute(
            """
            INSERT INTO python_test_scripts (requirement_id, script_role, script_text, raw_json)
            VALUES (%s, 'setup', %s, %s)
            """,
            (req_db_id, python_setup, json.dumps({"source": "python_test_setup"})),
        )
        stats["created"] += 1

    # test_cases
    for tc in raw_json.get("test_cases", []):
        tc_id = tc.get("test_case_id", "")
        title = tc.get("title", "")
        steps = tc.get("steps", [])
        steps_json = steps if isinstance(steps, list) else [str(steps)]
        python_script = tc.get("python_test_script", "")

        cur.execute(
            """
            INSERT INTO test_cases
                (requirement_id, test_case_id, title, precondition, steps,
                 expected_result, python_test_script, raw_json)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (requirement_id, test_case_id) DO UPDATE
                SET title              = EXCLUDED.title,
                    precondition       = EXCLUDED.precondition,
                    steps              = EXCLUDED.steps,
                    expected_result    = EXCLUDED.expected_result,
                    python_test_script = EXCLUDED.python_test_script,
                    raw_json           = EXCLUDED.raw_json
            """,
            (
                req_db_id, tc_id, title,
                tc.get("precondition", ""),
                json.dumps(steps_json),
                tc.get("expected_result", ""),
                python_script or None,
                json.dumps(tc),
            ),
        )
        stats["created"] += 1

        if python_script:
            cur.execute(
                """
                INSERT INTO python_test_scripts (requirement_id, script_role, script_text, raw_json)
                VALUES (%s, 'test_case', %s, %s)
                """,
                (req_db_id, python_script, json.dumps({"test_case_id": tc_id})),
            )
            stats["created"] += 1

    cur.execute(
        """
        INSERT INTO ingestion_job_items (ingestion_job_id, source_file_id, relative_path, status, detail)
        VALUES (%s, %s, %s, 'processed', %s)
        """,
        (job_id, sf_id, rel_path, f"req_id={req_id} req_db_id={req_db_id}"),
    )
    log.info("  Pytest %s → req_db_id=%d created=%d", rel_path, req_db_id, stats["created"])
    return stats


# ─── Step 4–5: Build RAG documents and chunks ─────────────────────────────────

def _upsert_rag_document(
    cur,
    dv_id: int,
    sf_id: Optional[int],
    source_type: str,
    source_entity_type: str,
    source_entity_id: Optional[int],
    title: str,
    text: str,
    metadata: dict,
) -> Optional[int]:
    """Insert or skip a rag_documents row. Returns rag_doc_id."""
    doc_hash = _sha256(text)
    cur.execute(
        """
        INSERT INTO rag_documents
            (dataset_version_id, source_file_id, source_type, source_entity_type,
             source_entity_id, document_title, document_text, document_hash, metadata)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (dataset_version_id, source_type, document_hash) DO NOTHING
        RETURNING id
        """,
        (
            dv_id, sf_id, source_type, source_entity_type,
            source_entity_id, title[:255] if title else None,
            text, doc_hash, json.dumps(metadata),
        ),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    # Fetch existing
    cur.execute(
        "SELECT id FROM rag_documents WHERE dataset_version_id=%s AND source_type=%s AND document_hash=%s",
        (dv_id, source_type, doc_hash),
    )
    existing = cur.fetchone()
    return existing[0] if existing else None


def _insert_chunks(cur, rag_doc_id: int, text: str, base_metadata: dict) -> int:
    """Chunk *text*, insert into rag_chunks, return number of new chunks."""
    chunks = _chunk_text(text)
    count = 0
    for idx, chunk_text in enumerate(chunks):
        chunk_hash = _sha256(chunk_text)
        token_count = len(chunk_text) // 4  # rough estimate
        meta = {**base_metadata, "chunk_index": idx, "total_chunks": len(chunks)}
        cur.execute(
            """
            INSERT INTO rag_chunks (rag_document_id, chunk_index, chunk_text, chunk_hash, token_count, metadata)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (chunk_hash) DO NOTHING
            """,
            (rag_doc_id, idx, chunk_text, chunk_hash, token_count, json.dumps(meta)),
        )
        count += cur.rowcount
    return count


def _build_rag_documents_for_version(cur, dv_id: int, dv_name: str) -> int:
    """Build rag_documents + rag_chunks for all structured data in *dv_id*. Returns chunk count."""
    total_chunks = 0

    # ── CAPL scripts → source_type='capl_script' ──────────────────────────
    cur.execute(
        """
        SELECT cs.id, cs.requirement_id, cs.requirement_text, cs.capl_script,
               cs.capl_pattern, cd.source_file_id
        FROM capl_scripts cs
        JOIN capl_documents cd ON cd.id = cs.capl_document_id
        WHERE cd.dataset_version_id = %s
        """,
        (dv_id,),
    )
    for cs_id, req_id, req_text, capl_script, pattern, sf_id in cur.fetchall():
        text = _capl_doc_text(req_id, req_text or "", capl_script)
        meta = {
            "dataset_version": dv_name,
            "requirement_id": req_id,
            "capl_pattern": pattern or "",
            "source_type": "capl_script",
        }
        doc_id = _upsert_rag_document(
            cur, dv_id, sf_id, "capl_script", "capl_script", cs_id,
            f"CAPL:{req_id}", text, meta,
        )
        if doc_id:
            total_chunks += _insert_chunks(cur, doc_id, text, meta)

    # ── CAN messages → source_type='capl_message' ─────────────────────────
    cur.execute(
        """
        SELECT cm.id, cm.name, cm.frame_id, cm.senders, cm.raw_payload, cd.source_file_id,
               COALESCE(
                   (SELECT json_agg(json_build_object(
                       'name', s.name, 'start', s.start_bit, 'length', s.signal_length,
                       'byte_order', s.byte_order, 'unit', s.unit
                   ))
                    FROM can_signals s WHERE s.can_message_id = cm.id),
                   '[]'::json
               ) AS signals_json
        FROM can_messages cm
        JOIN capl_documents cd ON cd.id = cm.capl_document_id
        WHERE cd.dataset_version_id = %s
        """,
        (dv_id,),
    )
    for cm_id, name, frame_id, senders, raw_payload, sf_id, signals_json in cur.fetchall():
        msg_dict = raw_payload or {}
        if isinstance(msg_dict, str):
            msg_dict = json.loads(msg_dict)
        msg_dict["signals"] = signals_json if isinstance(signals_json, list) else json.loads(signals_json or "[]")
        msg_dict["senders"] = senders or []
        text = _msg_doc_text(msg_dict, dv_name)
        meta = {
            "dataset_version": dv_name,
            "message_name": name,
            "frame_id": str(frame_id),
            "source_type": "capl_message",
        }
        doc_id = _upsert_rag_document(
            cur, dv_id, sf_id, "capl_message", "can_message", cm_id,
            f"MSG:{name}", text, meta,
        )
        if doc_id:
            total_chunks += _insert_chunks(cur, doc_id, text, meta)

    # ── Requirements → source_type='requirement' ──────────────────────────
    cur.execute(
        """
        SELECT r.id, r.requirement_id, r.description, r.python_test_setup,
               r.source_file_id,
               COALESCE(
                   (SELECT json_agg(json_build_object(
                       'node', rcm.node_name,
                       'arbitration_id', rcm.arbitration_id,
                       'signal', rcm.signal_name,
                       'bit', rcm.bit_position
                   ))
                    FROM requirement_can_messages rcm WHERE rcm.requirement_id = r.id),
                   '[]'::json
               ) AS can_msgs_json
        FROM requirements r
        WHERE r.dataset_version_id = %s
        """,
        (dv_id,),
    )
    for r_id, req_id, desc, py_setup, sf_id, can_msgs_json in cur.fetchall():
        can_msgs = can_msgs_json if isinstance(can_msgs_json, list) else json.loads(can_msgs_json or "[]")
        text = _req_doc_text(req_id, desc, can_msgs, py_setup)
        meta = {
            "dataset_version": dv_name,
            "requirement_id": req_id,
            "source_type": "requirement",
        }
        doc_id = _upsert_rag_document(
            cur, dv_id, sf_id, "requirement", "requirement", r_id,
            f"REQ:{req_id}", text, meta,
        )
        if doc_id:
            total_chunks += _insert_chunks(cur, doc_id, text, meta)

    # ── Test cases → source_type='test_case' ─────────────────────────────
    cur.execute(
        """
        SELECT tc.id, tc.test_case_id, tc.title, tc.precondition,
               tc.steps, tc.expected_result,
               r.requirement_id, r.source_file_id
        FROM test_cases tc
        JOIN requirements r ON r.id = tc.requirement_id
        WHERE r.dataset_version_id = %s
        """,
        (dv_id,),
    )
    for tc_id, tc_code, title, pre, steps_json, expected, req_id, sf_id in cur.fetchall():
        steps = steps_json if isinstance(steps_json, list) else json.loads(steps_json or "[]")
        tc_dict = {
            "test_case_id": tc_code, "title": title,
            "precondition": pre, "steps": steps, "expected_result": expected,
        }
        text = _tc_doc_text(req_id, tc_dict)
        meta = {
            "dataset_version": dv_name,
            "requirement_id": req_id,
            "test_case_id": tc_code,
            "source_type": "test_case",
        }
        doc_id = _upsert_rag_document(
            cur, dv_id, sf_id, "test_case", "test_case", tc_id,
            f"TC:{tc_code}", text, meta,
        )
        if doc_id:
            total_chunks += _insert_chunks(cur, doc_id, text, meta)

    log.info("  %s → %d new rag_chunks", dv_name, total_chunks)
    return total_chunks


# ─── Ingestion job bookkeeping ─────────────────────────────────────────────────

def _create_ingestion_job(cur, job_type: str, triggered_by: str) -> int:
    cur.execute(
        """
        INSERT INTO ingestion_jobs (job_type, status, triggered_by, started_at)
        VALUES (%s, 'running', %s, %s)
        RETURNING id
        """,
        (job_type, triggered_by, _now()),
    )
    return cur.fetchone()[0]


def _finish_ingestion_job(
    cur, job_id: int, stats: dict, errors: List[str]
) -> None:
    status = "failed" if stats.get("files_processed", 0) == 0 and errors else (
        "partial" if errors else "completed"
    )
    cur.execute(
        """
        UPDATE ingestion_jobs
        SET status           = %s,
            completed_at     = %s,
            files_discovered = %s,
            files_processed  = %s,
            records_created  = %s,
            records_skipped  = %s,
            error_summary    = %s
        WHERE id = %s
        """,
        (
            status, _now(),
            stats.get("files_discovered", 0),
            stats.get("files_processed", 0),
            stats.get("records_created", 0),
            stats.get("records_skipped", 0),
            ("\n".join(errors) if errors else None),
            job_id,
        ),
    )


# ─── Public entry point ────────────────────────────────────────────────────────

def run_ingestion(
    base_dir: Optional[Path] = None,
    job_type: str = "full_refresh",
    triggered_by: str = "script",
    skip_qdrant: bool = False,
) -> dict:
    """
    Run the full ingestion pipeline.

    Parameters
    ----------
    base_dir:     Repo root. Defaults to the directory three levels above this file.
    job_type:     'full_refresh' | 'incremental' | 'validation'
    triggered_by: Label stored in the job row.
    skip_qdrant:  If True, skip Qdrant embedding sync (useful for schema-only testing).

    Returns
    -------
    dict with keys: files_discovered, files_processed, records_created,
                    records_skipped, errors
    """
    base_dir = base_dir or BASE_DIR
    log.info("Starting ingestion (%s) from %s", job_type, base_dir)

    conn = get_connection()
    cur = conn.cursor()
    errors: List[str] = []
    stats = {
        "files_discovered": 0,
        "files_processed": 0,
        "records_created": 0,
        "records_skipped": 0,
    }

    job_id = _create_ingestion_job(cur, job_type, triggered_by)
    conn.commit()

    try:
        # ── Step 1 ────────────────────────────────────────────────────────
        dataset_versions = _discover_dataset_versions(cur, base_dir)
        conn.commit()

        for dv_name, dv_id in dataset_versions:
            dv_path = base_dir / dv_name
            log.info("Processing %s (id=%d)", dv_name, dv_id)

            # ── Step 2: Discover files ─────────────────────────────────────
            capl_files = sorted(dv_path.glob("CAPL_Data_*.json"))
            pytest_files = sorted(dv_path.rglob("pytest_data_REQ_*.json"))
            all_files = [(p, "capl_json") for p in capl_files] + [(p, "pytest_json") for p in pytest_files]
            stats["files_discovered"] += len(all_files)

            for file_path, file_type in all_files:
                rel_path = str(file_path.relative_to(base_dir))
                result = _register_source_file(cur, job_id, dv_id, dv_name, file_path, base_dir, file_type)
                if result is None:
                    errors.append(f"Failed to read {rel_path}")
                    continue

                sf_id, raw_json, is_new = result

                if not is_new and job_type == "incremental":
                    log.info("  Skipping unchanged %s", rel_path)
                    cur.execute(
                        """
                        INSERT INTO ingestion_job_items (ingestion_job_id, source_file_id, relative_path, status, detail)
                        VALUES (%s, %s, %s, 'skipped', 'unchanged hash')
                        """,
                        (job_id, sf_id, rel_path),
                    )
                    stats["records_skipped"] += 1
                    continue

                # ── Step 3: Parse structured data ─────────────────────────
                try:
                    if file_type == "capl_json":
                        s = _process_capl_file(cur, job_id, sf_id, dv_id, dv_name, raw_json, rel_path)
                    else:
                        s = _process_pytest_file(cur, job_id, sf_id, dv_id, dv_name, raw_json, rel_path)
                    stats["records_created"] += s.get("created", 0)
                    stats["records_skipped"] += s.get("skipped", 0)
                    stats["files_processed"] += 1
                except Exception as exc:
                    log.exception("Error processing %s: %s", rel_path, exc)
                    errors.append(f"{rel_path}: {exc}")
                    cur.execute(
                        """
                        INSERT INTO ingestion_job_items (ingestion_job_id, source_file_id, relative_path, status, detail)
                        VALUES (%s, %s, %s, 'failed', %s)
                        """,
                        (job_id, sf_id, rel_path, str(exc)),
                    )

            conn.commit()

            # ── Steps 4–5: RAG documents + chunks ─────────────────────────
            try:
                new_chunks = _build_rag_documents_for_version(cur, dv_id, dv_name)
                stats["records_created"] += new_chunks
                conn.commit()
            except Exception as exc:
                log.exception("RAG build failed for %s: %s", dv_name, exc)
                errors.append(f"RAG {dv_name}: {exc}")
                conn.rollback()

        # ── Step 6: Qdrant sync ────────────────────────────────────────────
        if not skip_qdrant:
            try:
                from services.data_pipeline.rag_sync import sync_pending_chunks
                synced, failed = sync_pending_chunks(conn)
                log.info("Qdrant sync: %d synced, %d failed", synced, failed)
                stats["records_created"] += synced
                if failed:
                    errors.append(f"Qdrant sync: {failed} chunks failed")
                conn.commit()
            except Exception as exc:
                log.exception("Qdrant sync error: %s", exc)
                errors.append(f"Qdrant sync: {exc}")
                conn.rollback()

        # ── Step 7: Finalize job ───────────────────────────────────────────
        _finish_ingestion_job(cur, job_id, stats, errors)
        conn.commit()

    except Exception as exc:
        log.exception("Fatal ingestion error: %s", exc)
        errors.append(str(exc))
        conn.rollback()
        cur.execute(
            "UPDATE ingestion_jobs SET status='failed', completed_at=%s, error_summary=%s WHERE id=%s",
            (_now(), str(exc), job_id),
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()

    stats["errors"] = errors
    log.info(
        "Ingestion complete: discovered=%d processed=%d created=%d skipped=%d errors=%d",
        stats["files_discovered"], stats["files_processed"],
        stats["records_created"], stats["records_skipped"], len(errors),
    )
    return stats


# ─── CLI ──────────────────────────────────────────────────────────────────────

def _cli() -> None:
    parser = argparse.ArgumentParser(description="ECU Testing AI — PostgreSQL ingestion pipeline")
    parser.add_argument(
        "--mode",
        choices=["full_refresh", "incremental", "validation"],
        default="full_refresh",
        help="Ingestion mode (default: full_refresh)",
    )
    parser.add_argument(
        "--base-dir",
        default=None,
        help="Repo root directory (defaults to auto-detected)",
    )
    parser.add_argument(
        "--skip-qdrant",
        action="store_true",
        help="Skip Qdrant embedding sync (schema/relational data only)",
    )
    args = parser.parse_args()
    base = Path(args.base_dir) if args.base_dir else None
    result = run_ingestion(base_dir=base, job_type=args.mode, skip_qdrant=args.skip_qdrant)
    if result.get("errors"):
        print("\nErrors:", file=sys.stderr)
        for e in result["errors"]:
            print(f"  {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    _cli()
