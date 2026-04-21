"""
Qdrant sync: reads pending rag_chunks from PostgreSQL, creates embeddings,
upserts to Qdrant, and records sync status in rag_chunk_sync.

Usage (standalone):
    python -m services.data_pipeline.rag_sync [--batch-size 64] [--collection rag_ecu]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import List, Optional, Tuple

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ── Defaults (overridable via env vars) ────────────────────────────────────────

DEFAULT_COLLECTION = os.environ.get("QDRANT_COLLECTION", "rag_ecu")
DEFAULT_EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-code-v1")
DEFAULT_QDRANT_PATH = os.environ.get("QDRANT_PATH", "./qdrant_data")
DEFAULT_BATCH_SIZE = 32


# ── Lazy imports (same pattern as rag_vector_store_qdrant.py) ─────────────────

def _load_qdrant():
    try:
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, PointStruct, VectorParams
        return QdrantClient, Distance, PointStruct, VectorParams
    except ImportError:
        raise ImportError("qdrant-client is required: pip install qdrant-client")


def _load_encoder(model_name: str):
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        raise ImportError("sentence-transformers is required: pip install sentence-transformers")
    trust = "bge-code" in model_name.lower()
    return SentenceTransformer(model_name, trust_remote_code=trust)


# ── Qdrant client factory (reuses rag_vector_store_qdrant logic) ───────────────

def _build_qdrant_client():
    host = os.environ.get("QDRANT_HOST", "").strip()
    port_str = os.environ.get("QDRANT_PORT", "").strip()
    path = os.environ.get("QDRANT_PATH", DEFAULT_QDRANT_PATH).strip()

    QdrantClient, Distance, PointStruct, VectorParams = _load_qdrant()

    if host:
        port = int(port_str) if port_str else 6333
        log.info("Connecting to Qdrant server %s:%d", host, port)
        return QdrantClient(host=host, port=port)

    os.makedirs(path, exist_ok=True)
    log.info("Using local Qdrant storage at %s", path)
    return QdrantClient(path=path)


def _ensure_qdrant_collection(client, collection_name: str, vector_size: int) -> None:
    QdrantClient, Distance, PointStruct, VectorParams = _load_qdrant()
    exists = False
    try:
        exists = bool(client.collection_exists(collection_name))
    except Exception:
        pass
    if exists:
        try:
            info = client.get_collection(collection_name)
            current_size = info.config.params.vectors.size
            if int(current_size) != int(vector_size):
                client.delete_collection(collection_name)
                exists = False
        except Exception:
            return
    if not exists:
        client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
        )
        log.info("Created Qdrant collection '%s' dim=%d", collection_name, vector_size)


# ── Core sync logic ────────────────────────────────────────────────────────────

def sync_pending_chunks(
    conn,
    collection_name: str = DEFAULT_COLLECTION,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> Tuple[int, int]:
    """
    Fetch rag_chunks that have no rag_chunk_sync row (pending) or status='stale',
    embed them, upsert to Qdrant, and record results in rag_chunk_sync.

    Returns (synced_count, failed_count).
    """
    cur = conn.cursor()

    # Load encoder once to get embedding dim
    log.info("Loading embedding model: %s", embedding_model)
    encoder = _load_encoder(embedding_model)
    try:
        dim = int(encoder.get_sentence_embedding_dimension())
    except Exception:
        dim = len(encoder.encode(["dim_probe"])[0])

    qdrant = _build_qdrant_client()
    _ensure_qdrant_collection(qdrant, collection_name, dim)
    _, _, PointStruct, _ = _load_qdrant()

    # Fetch chunks that need syncing
    cur.execute(
        """
        SELECT rc.id, rc.chunk_text, rc.chunk_hash, rc.metadata,
               rd.dataset_version_id, rd.source_type, rd.metadata AS doc_meta
        FROM rag_chunks rc
        JOIN rag_documents rd ON rd.id = rc.rag_document_id
        WHERE NOT EXISTS (
            SELECT 1 FROM rag_chunk_sync rcs
            WHERE rcs.rag_chunk_id = rc.id
              AND rcs.collection_name = %s
              AND rcs.sync_status IN ('synced')
        )
        ORDER BY rc.id
        """,
        (collection_name,),
    )
    pending = cur.fetchall()
    log.info("Chunks pending Qdrant sync: %d", len(pending))

    synced = 0
    failed = 0
    batch_start = 0

    while batch_start < len(pending):
        batch = pending[batch_start : batch_start + batch_size]
        texts = [row[1] for row in batch]

        try:
            vectors = encoder.encode(texts, show_progress_bar=False, convert_to_numpy=True).tolist()
        except Exception as exc:
            log.error("Embedding failed for batch at offset %d: %s", batch_start, exc)
            for row in batch:
                _record_sync_failure(cur, row[0], collection_name, embedding_model, str(exc))
                failed += 1
            batch_start += batch_size
            conn.commit()
            continue

        points = []
        chunk_ids = []
        point_ids = []

        for i, (chunk_id, chunk_text, chunk_hash, chunk_meta, dv_id, source_type, doc_meta) in enumerate(batch):
            point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"chunk:{chunk_id}"))
            chunk_meta_dict = chunk_meta if isinstance(chunk_meta, dict) else json.loads(chunk_meta or "{}")
            doc_meta_dict = doc_meta if isinstance(doc_meta, dict) else json.loads(doc_meta or "{}")

            payload = {
                "rag_chunk_id":      chunk_id,
                "dataset_version":   doc_meta_dict.get("dataset_version", ""),
                "source_type":       source_type,
                "requirement_id":    chunk_meta_dict.get("requirement_id", ""),
                "message_name":      chunk_meta_dict.get("message_name", ""),
                "capl_pattern":      chunk_meta_dict.get("capl_pattern", ""),
                "test_case_id":      chunk_meta_dict.get("test_case_id", ""),
                "chunk_hash":        chunk_hash,
                "chunk_index":       chunk_meta_dict.get("chunk_index", 0),
                "content_preview":   chunk_text[:4000],
                # Aliases expected by the existing RAGVectorStore.retrieve() filters
                "source":            _source_type_to_rag_source(source_type),
            }
            points.append(PointStruct(id=point_id, vector=vectors[i], payload=payload))
            chunk_ids.append(chunk_id)
            point_ids.append(point_id)

        try:
            qdrant.upsert(collection_name=collection_name, points=points)
            now = datetime.now(tz=timezone.utc)
            for chunk_id, point_id in zip(chunk_ids, point_ids):
                cur.execute(
                    """
                    INSERT INTO rag_chunk_sync
                        (rag_chunk_id, vector_store, collection_name, point_id,
                         embedding_model, sync_status, synced_at)
                    VALUES (%s, 'qdrant', %s, %s, %s, 'synced', %s)
                    ON CONFLICT (rag_chunk_id, vector_store, collection_name) DO UPDATE
                        SET point_id      = EXCLUDED.point_id,
                            sync_status   = 'synced',
                            synced_at     = EXCLUDED.synced_at,
                            error_message = NULL
                    """,
                    (chunk_id, collection_name, point_id, embedding_model, now),
                )
            synced += len(points)
            log.info("  Upserted batch of %d chunks to Qdrant", len(points))
        except Exception as exc:
            log.error("Qdrant upsert failed for batch at offset %d: %s", batch_start, exc)
            for chunk_id in chunk_ids:
                _record_sync_failure(cur, chunk_id, collection_name, embedding_model, str(exc))
                failed += 1

        conn.commit()
        batch_start += batch_size

    cur.close()
    return synced, failed


def mark_chunks_stale(conn, dataset_version_id: int, collection_name: str = DEFAULT_COLLECTION) -> int:
    """Mark all synced chunks for a dataset version as stale (used before full refresh)."""
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE rag_chunk_sync rcs
        SET sync_status = 'stale'
        FROM rag_chunks rc
        JOIN rag_documents rd ON rd.id = rc.rag_document_id
        WHERE rcs.rag_chunk_id = rc.id
          AND rcs.collection_name = %s
          AND rd.dataset_version_id = %s
          AND rcs.sync_status = 'synced'
        """,
        (collection_name, dataset_version_id),
    )
    count = cur.rowcount
    conn.commit()
    cur.close()
    return count


def _record_sync_failure(cur, chunk_id: int, collection_name: str, model: str, error: str) -> None:
    cur.execute(
        """
        INSERT INTO rag_chunk_sync
            (rag_chunk_id, vector_store, collection_name, point_id, embedding_model, sync_status, error_message)
        VALUES (%s, 'qdrant', %s, %s, %s, 'failed', %s)
        ON CONFLICT (rag_chunk_id, vector_store, collection_name) DO UPDATE
            SET sync_status   = 'failed',
                error_message = EXCLUDED.error_message
        """,
        (chunk_id, collection_name, f"pending_{chunk_id}", model, error[:1000]),
    )


def _source_type_to_rag_source(source_type: str) -> str:
    """Map DB source_type to the 'source' field tag expected by RAGVectorStore filters."""
    return {
        "capl_script":  "capl",
        "capl_message": "dbc",
        "capl_signal":  "dbc",
        "requirement":  "requirement",
        "test_case":    "test_case",
        "python_setup": "python",
        "python_script":"python",
    }.get(source_type, source_type)


# ── CLI ────────────────────────────────────────────────────────────────────────

def _cli() -> None:
    parser = argparse.ArgumentParser(description="ECU Testing AI — Qdrant sync")
    parser.add_argument("--collection", default=DEFAULT_COLLECTION)
    parser.add_argument("--model", default=DEFAULT_EMBEDDING_MODEL)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    args = parser.parse_args()

    from services.data_pipeline.db import get_connection
    conn = get_connection()
    try:
        synced, failed = sync_pending_chunks(
            conn,
            collection_name=args.collection,
            embedding_model=args.model,
            batch_size=args.batch_size,
        )
        print(f"Qdrant sync complete: synced={synced} failed={failed}")
        if failed:
            sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    _cli()
