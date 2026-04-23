"""
Qdrant-backed RAG vector store for ECU testing apps.

Uses Qdrant for vector storage and sentence-transformers for embeddings.
Provides the same interface as rag_vector_store.RAGVectorStore so that
complete_rag_app.py can use it without change.

By default uses in-memory Qdrant (no server needed). To use a Qdrant server, set QDRANT_HOST and/or QDRANT_PORT. Optional: QDRANT_PATH (e.g. :memory: or file path), QDRANT_COLLECTION.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from typing import Any, Dict, List, Optional, Tuple

from rag_vector_store import Chunk, RetrievalResult

# Maps Qdrant metadata 'source' tags → PostgreSQL rag_documents.source_type values.
# All values must be in the schema's CHECK constraint.
_PG_SOURCE_MAP: Dict[str, str] = {
    "requirement":              "requirement",
    "user_requirement":         "requirement",
    "dbc":                      "capl_message",
    "dbc_context":              "capl_message",
    "dbc_message":              "capl_message",
    "capl":                     "capl_script",
    "capl_implementation":      "capl_script",
    "test_case":                "test_case",
    "python":                   "python_script",
    "python_test_script":       "python_script",
    "python_test_script_full":  "python_script",
}

# Qdrant
try:
    from qdrant_client import QdrantClient
    from qdrant_client.models import (
        VectorParams,
        Distance,
        PointStruct,
        Filter,
        FieldCondition,
        MatchValue,
    )
except ImportError:
    QdrantClient = None  # type: ignore
    PointStruct = None  # type: ignore
    VectorParams = None  # type: ignore
    Distance = None  # type: ignore
    Filter = None  # type: ignore
    FieldCondition = None  # type: ignore
    MatchValue = None  # type: ignore

# Embeddings
try:
    from sentence_transformers import SentenceTransformer
except ImportError:
    SentenceTransformer = None  # type: ignore


# Default collection name and embedding model
DEFAULT_COLLECTION_NAME = "rag_ecu"
DEFAULT_EMBEDDING_MODEL = "all-MiniLM-L6-v2"
# Default persistent storage path (use None or ":memory:" for in-memory)
DEFAULT_QDRANT_PATH = "./qdrant_data"

# Process-global cache: one Qdrant client per (path or host:port) so we never open
# the same storage twice in one process (avoids "already accessed by another instance" on refresh).
_qdrant_client_cache: Dict[Tuple[str, ...], Any] = {}


def _get_qdrant_client(
    host: Optional[str] = None,
    port: Optional[int] = None,
    path: Optional[str] = None,
):
    """
    Create or reuse Qdrant client. Reuses the same client for the same path/host:port
    in this process so Streamlit refresh does not open the storage folder twice.
    """
    if QdrantClient is None:
        raise ImportError("Qdrant client is required. Install with: pip install qdrant-client")
    path_val = path
    if path_val is None:
        path_val = os.environ.get("QDRANT_PATH", DEFAULT_QDRANT_PATH)
    if path_val is not None and path_val.strip() and path_val.strip().lower() != ":memory:":
        path_val = path_val.strip()
        os.makedirs(path_val, exist_ok=True)
        cache_key = ("path", os.path.abspath(path_val))
        if cache_key not in _qdrant_client_cache:
            _qdrant_client_cache[cache_key] = QdrantClient(path=path_val)
        return _qdrant_client_cache[cache_key]
    if path_val is not None and path_val.strip().lower() == ":memory:":
        return QdrantClient(path=":memory:")
    # Use server only if user explicitly set host or port
    use_server = (
        (host is not None) or (port is not None)
        or os.environ.get("QDRANT_HOST") is not None
        or os.environ.get("QDRANT_PORT") is not None
    )
    if use_server:
        h = (host or os.environ.get("QDRANT_HOST") or "localhost").strip()
        p = port
        if p is None:
            try:
                p = int(os.environ.get("QDRANT_PORT", "6333"))
            except ValueError:
                p = 6333
        cache_key = ("server", h, p)
        if cache_key not in _qdrant_client_cache:
            _qdrant_client_cache[cache_key] = QdrantClient(host=h, port=p)
        return _qdrant_client_cache[cache_key]
    # Default: persistent local storage at DEFAULT_QDRANT_PATH
    os.makedirs(DEFAULT_QDRANT_PATH, exist_ok=True)
    cache_key = ("path", os.path.abspath(DEFAULT_QDRANT_PATH))
    if cache_key not in _qdrant_client_cache:
        _qdrant_client_cache[cache_key] = QdrantClient(path=DEFAULT_QDRANT_PATH)
    return _qdrant_client_cache[cache_key]


def _ensure_collection(client: Any, collection_name: str, vector_size: int) -> None:
    """Create collection if it does not exist. If it exists with wrong vector size, recreate it."""
    if VectorParams is None or Distance is None:
        raise ImportError("qdrant_client.models required")
    exists = False
    try:
        exists = bool(client.collection_exists(collection_name))
    except Exception:
        exists = False
    if exists:
        try:
            info = client.get_collection(collection_name)
            current_size = info.config.params.vectors.size  # type: ignore[attr-defined]
            if int(current_size) != int(vector_size):
                client.delete_collection(collection_name)
                exists = False
        except Exception:
            # If we can't inspect it, don't delete it; keep behavior stable.
            return
    if not exists:
        client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
        )


class RAGVectorStore:
    """
    Qdrant-backed RAG vector store with the same API as rag_vector_store.RAGVectorStore.
    Document content is kept in-memory (_id_to_content); vectors and filterable metadata live in Qdrant.
    """

    def __init__(
        self,
        cache_dir: str = "./rag_cache",
        host: Optional[str] = None,
        port: Optional[int] = None,
        path: Optional[str] = None,
        collection_name: Optional[str] = None,
        embedding_model: Optional[str] = None,
    ) -> None:
        self.cache_dir = cache_dir
        self._embedding_model_name = embedding_model or os.environ.get("EMBEDDING_MODEL") or DEFAULT_EMBEDDING_MODEL
        if SentenceTransformer is None:
            raise ImportError(
                "sentence_transformers is required for embeddings. Install with: pip install sentence-transformers"
            )
        # Some models (like bge-code-v1) require trust_remote_code=True
        trust_remote_code_env = (os.environ.get("ST_TRUST_REMOTE_CODE") or "").strip().lower()
        if trust_remote_code_env:
            trust_remote_code = trust_remote_code_env in ("1", "true", "yes", "on")
        else:
            # Sensible default: enable for known remote-code embedding models
            trust_remote_code = "bge-code" in self._embedding_model_name.lower()
        self._encoder = SentenceTransformer(self._embedding_model_name, trust_remote_code=trust_remote_code)
        try:
            self._embedding_dim: int = int(self._encoder.get_sentence_embedding_dimension())
        except Exception:
            # Fallback: infer from one embedding
            self._embedding_dim = len(self.encode(["dim_check"])[0])
        self._client = _get_qdrant_client(host=host, port=port, path=path)
        self._collection = (collection_name or os.environ.get("QDRANT_COLLECTION") or DEFAULT_COLLECTION_NAME).strip()
        _ensure_collection(self._client, self._collection, self._embedding_dim)
        self._id_to_content: Dict[str, str] = {}
        self._document_count: int = 0
        self._stats_by_type: Dict[str, int] = {}
        self._rehydrate_from_persistent()
        self.model = self
        self.collection = self

        # Optional PostgreSQL tracking (set via attach_db)
        self._pg_conn: Optional[Any] = None
        self._pg_dv_id: Optional[int] = None

    # ── PostgreSQL integration ─────────────────────────────────────────────────

    def attach_db(self, conn: Any, dv_id: int) -> None:
        """
        Attach a PostgreSQL connection so that every _add_document call also
        writes to rag_documents / rag_chunks / rag_chunk_sync.

        *conn* must be opened with autocommit=True (pg_bridge.init_for_app does this).
        """
        self._pg_conn = conn
        self._pg_dv_id = dv_id

    def _pg_write(self, text: str, source: str, point_id: str) -> None:
        """
        Persist one Qdrant chunk to PostgreSQL (rag_documents → rag_chunks →
        rag_chunk_sync).  Silently swallows all errors so the app never fails
        because of a DB issue.
        """
        if not self._pg_conn or not self._pg_dv_id:
            return
        try:
            pg_source_type = _PG_SOURCE_MAP.get(source, "external_doc")
            # Include dv_id+source_type in hash so identical text with different
            # source types gets distinct chunk rows rather than a silent collision.
            chunk_seed = f"{self._pg_dv_id}:{pg_source_type}:{text}"
            doc_hash = hashlib.sha256(chunk_seed.encode()).hexdigest()
            meta_json = json.dumps({"app_source": source, "qdrant_point_id": point_id})

            cur = self._pg_conn.cursor()

            # rag_documents ──────────────────────────────────────────────────
            cur.execute(
                """
                INSERT INTO rag_documents
                    (dataset_version_id, source_type, source_entity_type,
                     document_text, document_hash, metadata)
                VALUES (%s, %s, 'file', %s, %s, %s)
                ON CONFLICT (dataset_version_id, source_type, document_hash) DO NOTHING
                RETURNING id
                """,
                (self._pg_dv_id, pg_source_type, text, doc_hash, meta_json),
            )
            row = cur.fetchone()
            if not row:
                cur.execute(
                    "SELECT id FROM rag_documents "
                    "WHERE dataset_version_id=%s AND source_type=%s AND document_hash=%s",
                    (self._pg_dv_id, pg_source_type, doc_hash),
                )
                row = cur.fetchone()
            if not row:
                cur.close()
                return
            doc_id = row[0]

            # rag_chunks ─────────────────────────────────────────────────────
            cur.execute(
                """
                INSERT INTO rag_chunks
                    (rag_document_id, chunk_index, chunk_text, chunk_hash,
                     token_count, metadata)
                VALUES (%s, 0, %s, %s, %s, %s)
                ON CONFLICT (chunk_hash) DO NOTHING
                RETURNING id
                """,
                (doc_id, text, doc_hash, len(text) // 4, meta_json),
            )
            chunk_row = cur.fetchone()
            if not chunk_row:
                cur.execute("SELECT id FROM rag_chunks WHERE chunk_hash=%s", (doc_hash,))
                chunk_row = cur.fetchone()
            if not chunk_row:
                cur.close()
                return
            chunk_id = chunk_row[0]

            # rag_chunk_sync ─────────────────────────────────────────────────
            cur.execute(
                """
                INSERT INTO rag_chunk_sync
                    (rag_chunk_id, vector_store, collection_name, point_id,
                     embedding_model, sync_status, synced_at)
                VALUES (%s, 'qdrant', %s, %s, %s, 'synced', NOW())
                ON CONFLICT (rag_chunk_id, vector_store, collection_name) DO UPDATE
                    SET point_id      = EXCLUDED.point_id,
                        sync_status   = 'synced',
                        synced_at     = NOW(),
                        error_message = NULL
                """,
                (chunk_id, self._collection, point_id, self._embedding_model_name),
            )
            cur.close()
        except Exception as exc:
            print(f"[DEBUG PG] _pg_write non-fatal error: {exc}")

    # ── Encoding ──────────────────────────────────────────────────────────────

    def encode(self, texts: List[str], show_progress_bar: bool = False) -> List[List[float]]:
        """Encode texts to vectors using the sentence-transformers model."""
        if not texts:
            return []
        arr = self._encoder.encode(
            texts,
            show_progress_bar=show_progress_bar,
            convert_to_numpy=True,
        )
        return arr.tolist()

    def _payload_for_meta(self, meta: Dict[str, str], content: str) -> Dict[str, Any]:
        """Build Qdrant payload (all values must be str, int, float, bool, or list of those)."""
        payload = {k: str(v)[:1000] for k, v in meta.items()}
        payload["content_preview"] = (content[:4000] if content else "")
        return payload

    def add(
        self,
        embeddings: List[List[float]],
        documents: List[str],
        metadatas: List[Dict[str, str]],
        ids: List[str],
    ) -> None:
        """Upsert vectors to Qdrant and store document content locally."""
        if not ids or PointStruct is None:
            return
        points = []
        for i, (doc_id, doc, meta) in enumerate(zip(ids, documents, metadatas)):
            vec = embeddings[i] if i < len(embeddings) else self.encode([doc])[0]
            payload = self._payload_for_meta(meta, doc)
            points.append(PointStruct(id=doc_id, vector=vec, payload=payload))
            self._id_to_content[doc_id] = doc
        self._client.upsert(collection_name=self._collection, points=points)
        self._document_count += len(ids)
        for doc_id, doc, m in zip(ids, documents, metadatas):
            t = m.get("source") or m.get("type") or ""
            if t:
                self._stats_by_type[t] = self._stats_by_type.get(t, 0) + 1
            self._pg_write(doc, t, doc_id)

    def _add_document(self, text: str, base_metadata: Dict[str, str]) -> str:
        if not text.strip():
            return ""
        doc_id = str(uuid.uuid4())
        meta = {k: str(v) for k, v in base_metadata.items()}
        vec = self.encode([text])[0]
        payload = self._payload_for_meta(meta, text)
        self._client.upsert(
            collection_name=self._collection,
            points=[PointStruct(id=doc_id, vector=vec, payload=payload)],
        )
        self._id_to_content[doc_id] = text
        self._document_count += 1
        t = meta.get("source") or meta.get("type") or ""
        if t:
            self._stats_by_type[t] = self._stats_by_type.get(t, 0) + 1
        # Mirror to PostgreSQL when a DB connection is attached
        self._pg_write(text, t, doc_id)
        return doc_id

    def _rehydrate_from_persistent(self) -> None:
        """If Qdrant has existing points, load _id_to_content from payloads (for persistence across restarts)."""
        try:
            count_fn = getattr(self._client, "count", None) or getattr(self._client, "count_points", None)
            if count_fn:
                result = count_fn(collection_name=self._collection, exact=True)
                count = getattr(result, "count", 0) or 0
            else:
                count = 0
        except Exception:
            count = 0
        if count == 0:
            return
        print(f"[DEBUG RAG] _rehydrate_from_persistent: found {count} points in Qdrant, loading _id_to_content...")
        offset = None
        while True:
            points, next_offset = self._client.scroll(
                collection_name=self._collection,
                limit=100,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for pt in points:
                doc_id = str(pt.id) if pt.id is not None else ""
                payload = pt.payload or {}
                content = payload.get("content_preview", "")
                if doc_id and content:
                    self._id_to_content[doc_id] = content
                src = payload.get("source") or payload.get("type") or ""
                if src:
                    self._stats_by_type[src] = self._stats_by_type.get(src, 0) + 1
            self._document_count += len(points)
            if next_offset is None:
                break
            offset = next_offset
        print(f"[DEBUG RAG] _rehydrate_from_persistent: loaded {len(self._id_to_content)} chunks, _document_count={self._document_count}")

    def add_requirement(self, text: str, metadata: Optional[Dict] = None) -> str:
        meta = {"source": "requirement"}
        if metadata:
            meta.update({k: str(v) for k, v in metadata.items()})
        return self._add_document(text, meta)

    def add_dbc_context(self, text: str, metadata: Optional[Dict] = None) -> str:
        meta = {"source": "dbc"}
        if metadata:
            meta.update({k: str(v) for k, v in metadata.items()})
        return self._add_document(text, meta)

    def add_capl_script(self, text: str, metadata: Optional[Dict] = None) -> str:
        meta = {"source": "capl"}
        if metadata:
            meta.update({k: str(v) for k, v in metadata.items()})
        return self._add_document(text, meta)

    def _retrieve_with_filter(
        self,
        query_vec: List[float],
        top_k: int,
        source_filter: Optional[str],
        metadata_filter: Optional[Dict[str, Any]],
        source_aliases: Optional[List[str]] = None,
    ) -> RetrievalResult:
        """Internal: run search with given filters. Tries source_aliases if source_filter given."""
        filters_to_try: List[Tuple[Optional[str], Optional[Dict[str, Any]]]] = []
        if source_filter:
            aliases = source_aliases or [source_filter]
            for src in aliases:
                filters_to_try.append((src, metadata_filter))
        else:
            filters_to_try.append((None, metadata_filter))
        last_result = RetrievalResult(chunks=[], scores=[], context_text="")
        for src, meta_f in filters_to_try:
            must_conditions = []
            if src and Filter and FieldCondition and MatchValue:
                must_conditions.append(FieldCondition(key="source", match=MatchValue(value=src)))
            if meta_f and Filter and FieldCondition and MatchValue:
                for key, value in meta_f.items():
                    if value is not None and value != "":
                        must_conditions.append(FieldCondition(key=key, match=MatchValue(value=str(value))))
            query_filter = Filter(must=must_conditions) if must_conditions else None
            filter_desc = f"source={src}" if src else "no_filter"
            try:
                result = self._client.query_points(
                    collection_name=self._collection,
                    query=query_vec,
                    query_filter=query_filter,
                    limit=top_k,
                )
                hits = getattr(result, "points", None) or getattr(result, "result", None) or []
                print(f"[DEBUG RAG] _retrieve_with_filter: {filter_desc} -> {len(hits)} hits")
            except Exception as ex:
                print(f"[DEBUG RAG] _retrieve_with_filter: {filter_desc} -> Exception: {ex}")
                continue
            chunks, scores = [], []
            for h in hits:
                doc_id = str(h.id) if h.id is not None else ""
                payload = h.payload or {}
                content = self._id_to_content.get(doc_id) or payload.get("content_preview", "")
                meta = {k: str(v) for k, v in payload.items()}
                chunks.append(Chunk(id=doc_id, content=content, metadata=meta))
                scores.append(float(h.score or 0.0))
            last_result = RetrievalResult(
                chunks=chunks,
                scores=scores,
                context_text="\n\n".join(c.content for c in chunks),
            )
            if chunks:
                return last_result
        return last_result

    def retrieve(
        self,
        query: str,
        top_k: int = 3,
        source_filter: Optional[str] = None,
        metadata_filter: Optional[Dict[str, Any]] = None,
    ) -> RetrievalResult:
        """Query Qdrant by embedding the query. Uses source aliases and fallback to unfiltered if empty."""
        stats = self.get_stats()
        print(f"[DEBUG RAG] retrieve: store has total_docs={stats['total_documents']}, req={stats['requirement_chunks']}, dbc={stats['dbc_chunks']}, capl={stats['capl_scripts']}; query source_filter={source_filter!r} top_k={top_k}")
        query_vec = self.encode([query])[0]
        source_aliases_map = {
            "requirement": ["requirement", "user_requirement"],
            "dbc": ["dbc", "dbc_context", "dbc_message"],
            "capl": ["capl", "capl_implementation"],
            "test_case": ["test_case"],
            "python": ["python", "python_test_script", "python_test_script_full"],
        }
        aliases = source_aliases_map.get(source_filter or "", [source_filter]) if source_filter else None
        result = self._retrieve_with_filter(
            query_vec, top_k, source_filter, metadata_filter, source_aliases=aliases
        )
        if not result.chunks and source_filter:
            print(f"[DEBUG RAG] retrieve: filtered search returned 0, trying unfiltered fallback for source_filter={source_filter!r}")
            result = self._retrieve_with_filter(query_vec, top_k, None, None, source_aliases=None)
        print(f"[DEBUG RAG] retrieve: final result {len(result.chunks)} chunks for source_filter={source_filter!r}")
        return result

    def retrieve_both(
        self,
        query: str,
        req_k: int = 3,
        dbc_k: int = 5,
    ) -> Tuple[RetrievalResult, RetrievalResult]:
        req_res = self.retrieve(query, top_k=req_k, source_filter="requirement")
        dbc_res = self.retrieve(query, top_k=dbc_k, source_filter="dbc")
        return req_res, dbc_res

    def retrieve_all(
        self,
        query: str,
        req_k: int = 3,
        dbc_k: int = 5,
        capl_k: int = 2,
        capl_pattern: Optional[str] = None,
        req_metadata_filter: Optional[Dict[str, str]] = None,
    ) -> Tuple[RetrievalResult, RetrievalResult, RetrievalResult]:
        req_res = self.retrieve(
            query, top_k=req_k, source_filter="requirement",
            metadata_filter=req_metadata_filter,
        )
        if not req_res.chunks and req_metadata_filter:
            req_res = self.retrieve(query, top_k=req_k, source_filter="requirement")
        dbc_res = self.retrieve(query, top_k=dbc_k, source_filter="dbc")
        if capl_pattern:
            capl_res = self.retrieve(
                query, top_k=capl_k, source_filter="capl",
                metadata_filter={"capl_pattern": capl_pattern},
            )
            if not capl_res.chunks:
                capl_res = self.retrieve(query, top_k=capl_k, source_filter="capl")
        else:
            capl_res = self.retrieve(query, top_k=capl_k, source_filter="capl")
        return req_res, dbc_res, capl_res

    def clear(self) -> None:
        """Delete and recreate the collection, then clear local content cache."""
        # Mark all synced PostgreSQL chunks for this collection as stale
        if self._pg_conn and self._pg_dv_id:
            try:
                cur = self._pg_conn.cursor()
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
                    (self._collection, self._pg_dv_id),
                )
                cur.close()
            except Exception as exc:
                print(f"[DEBUG PG] clear stale-mark non-fatal: {exc}")
        try:
            if self._client.collection_exists(self._collection):
                self._client.delete_collection(self._collection)
            _ensure_collection(self._client, self._collection, self._embedding_dim)
        except Exception:
            pass
        self._id_to_content.clear()
        self._document_count = 0
        self._stats_by_type.clear()

    def build_index(self) -> None:
        """No-op; Qdrant index is managed on upsert."""

    def get_stats(self) -> Dict[str, int]:
        """Return basic statistics. Uses in-memory counts updated on add/clear."""
        req_count = self._stats_by_type.get("requirement", 0) + self._stats_by_type.get("user_requirement", 0)
        dbc_count = self._stats_by_type.get("dbc", 0) + self._stats_by_type.get("dbc_context", 0)
        capl_count = self._stats_by_type.get("capl", 0) + self._stats_by_type.get("capl_implementation", 0)
        return {
            "total_documents": self._document_count,
            "requirement_chunks": req_count,
            "dbc_chunks": dbc_count,
            "capl_scripts": capl_count,
            "embedding_dimension": self._embedding_dim,
        }


class ExtendedRAGVectorStore(RAGVectorStore):
    """
    Qdrant-backed extended RAG store with test cases and Python scripts support.
    Same API as the extended store used by complete_rag_app.
    """

    def add_test_case(self, test_case_text: str, metadata: Optional[Dict] = None) -> Optional[str]:
        if not test_case_text.strip():
            return None
        base_metadata = metadata or {}
        embedding = self.model.encode([test_case_text], show_progress_bar=False)[0]
        doc_id = str(uuid.uuid4())
        payload = self._payload_for_meta(
            {"source": "test_case", **{k: str(v) for k, v in base_metadata.items()}},
            test_case_text,
        )
        payload = {k: (str(v)[:1000] if k != "content_preview" else v) for k, v in payload.items()}
        self._client.upsert(
            collection_name=self._collection,
            points=[PointStruct(id=doc_id, vector=embedding, payload=payload)],
        )
        self._id_to_content[doc_id] = test_case_text
        self._document_count += 1
        self._stats_by_type["test_case"] = self._stats_by_type.get("test_case", 0) + 1
        self._pg_write(test_case_text, "test_case", doc_id)
        return doc_id

    def add_python_script(self, python_script: str, metadata: Optional[Dict] = None) -> Optional[str]:
        if not python_script.strip():
            return None
        base_metadata = metadata or {}
        embedding = self.model.encode([python_script], show_progress_bar=False)[0]
        doc_id = str(uuid.uuid4())
        payload = self._payload_for_meta(
            {"source": "python", **{k: str(v) for k, v in base_metadata.items()}},
            python_script,
        )
        payload = {k: (str(v)[:1000] if k != "content_preview" else v) for k, v in payload.items()}
        self._client.upsert(
            collection_name=self._collection,
            points=[PointStruct(id=doc_id, vector=embedding, payload=payload)],
        )
        self._id_to_content[doc_id] = python_script
        self._document_count += 1
        self._stats_by_type["python"] = self._stats_by_type.get("python", 0) + 1
        self._pg_write(python_script, "python", doc_id)
        return doc_id

    def retrieve_test_cases(self, query: str, top_k: int = 3, requirement_id: Optional[str] = None) -> RetrievalResult:
        """Retrieve test case examples by semantic similarity. requirement_id is kept for API compatibility but not used."""
        return self.retrieve(query, top_k=top_k, source_filter="test_case")

    def retrieve_python_scripts(self, query: str, top_k: int = 2, requirement_id: Optional[str] = None) -> RetrievalResult:
        """Retrieve Python script examples by semantic similarity. requirement_id is kept for API compatibility but not used."""
        return self.retrieve(query, top_k=top_k, source_filter="python")


# Re-export for drop-in use
__all__ = ["RAGVectorStore", "ExtendedRAGVectorStore", "Chunk", "RetrievalResult"]
