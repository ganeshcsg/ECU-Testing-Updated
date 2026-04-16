"""
Lightweight RAG vector store used by CAPL/RAG apps.

This is a simplified in‑memory implementation that provides the same
interface expected by:

- complete_rag_app.py
- rag_extended.py

It does NOT depend on external vector DBs; instead it uses a very simple
token‑based similarity for retrieval. This is sufficient for development
and for demonstrating the RAG workflows.
"""

from __future__ import annotations

import os
import math
import shutil
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Iterable


# =============================================================================
# DATA STRUCTURES
# =============================================================================


@dataclass
class Chunk:
    """Single stored document/chunk in the vector store."""

    id: str
    content: str
    metadata: Dict[str, str]


@dataclass
class RetrievalResult:
    """Result of a retrieval call."""

    chunks: List[Chunk]
    scores: List[float]
    context_text: str


# =============================================================================
# SIMPLE TOKEN-BASED "EMBEDDING" AND SIMILARITY
# =============================================================================


def _tokenize(text: str) -> List[str]:
    """Very simple tokenizer: lowercase and split on whitespace."""
    return [t for t in text.lower().split() if t]


def _build_vector(text: str) -> Dict[str, float]:
    """
    Build a very small bag‑of‑words vector as a dict token -> count.
    """
    vec: Dict[str, float] = {}
    for tok in _tokenize(text):
        vec[tok] = vec.get(tok, 0.0) + 1.0
    return vec


def _cosine_similarity(a: Dict[str, float], b: Dict[str, float]) -> float:
    """Cosine similarity between two sparse bag‑of‑words vectors."""
    if not a or not b:
        return 0.0

    # Dot product
    dot = 0.0
    for k, va in a.items():
        vb = b.get(k)
        if vb:
            dot += va * vb

    if dot == 0.0:
        return 0.0

    # Norms
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if na == 0.0 or nb == 0.0:
        return 0.0

    return dot / (na * nb)


# =============================================================================
# CORE VECTOR STORE
# =============================================================================


class RAGVectorStore:
    """
    Extremely simple in‑memory RAG store.

    API surface intentionally mirrors the original project so that:
    - app14 1.py
    - complete_rag_app.py
    - rag_extended.ExtendedRAGVectorStore
    can all use it without modification.
    """

    def __init__(self, cache_dir: str = "./rag_cache") -> None:
        self.cache_dir = cache_dir
        os.makedirs(self.cache_dir, exist_ok=True)

        self._documents: List[Chunk] = []
        self._document_count: int = 0

        # "Embedding model" placeholder – we keep an API compatible attribute
        # so ExtendedRAGVectorStore can call self.model.encode(...)
        self.model = self  # type: ignore

        # Chroma‑like collection placeholder. We just proxy to internal list.
        self.collection = self  # type: ignore

        # Chosen arbitrarily; used only in get_stats()
        self._embedding_dim: int = 384

    # ---------------------------------------------------------------------
    # SentenceTransformer‑like encode API (for ExtendedRAGVectorStore)
    # ---------------------------------------------------------------------

    def encode(self, texts: List[str], show_progress_bar: bool = False) -> List[List[float]]:
        """
        Very small stand‑in for a real embedding model.
        We do not actually store these embeddings; ExtendedRAGVectorStore
        just needs some numeric vectors to satisfy its contract when adding
        test cases / python scripts.
        """
        # Map bag‑of‑words into a fixed‑size dense vector via hashing.
        # This is intentionally simple.
        vectors: List[List[float]] = []
        for text in texts:
            vec = [0.0] * self._embedding_dim
            for tok in _tokenize(text):
                h = hash(tok) % self._embedding_dim
                vec[h] += 1.0
            vectors.append(vec)
        return vectors

    # ---------------------------------------------------------------------
    # Chroma‑like collection.add API (for ExtendedRAGVectorStore)
    # ---------------------------------------------------------------------

    def add(
        self,
        embeddings: List[List[float]],
        documents: List[str],
        metadatas: List[Dict[str, str]],
        ids: List[str],
    ) -> None:
        """
        Pretend to be a Chroma collection.add(). We ignore the embeddings and
        simply store documents + metadata + ids internally.
        """
        for doc_id, doc, meta in zip(ids, documents, metadatas):
            self._documents.append(Chunk(id=doc_id, content=doc, metadata=meta))
            self._document_count += 1

    # ---------------------------------------------------------------------
    # High‑level document add helpers (used throughout the app code)
    # ---------------------------------------------------------------------

    def _add_document(self, text: str, base_metadata: Dict[str, str]) -> str:
        if not text.strip():
            return ""
        doc_id = str(uuid.uuid4())
        meta = {k: str(v) for k, v in base_metadata.items()}
        self._documents.append(Chunk(id=doc_id, content=text, metadata=meta))
        self._document_count += 1
        return doc_id

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

    # ---------------------------------------------------------------------
    # Retrieval
    # ---------------------------------------------------------------------

    def _iter_filtered_documents(
        self,
        source_filter: Optional[str] = None,
    ) -> Iterable[Chunk]:
        if not source_filter:
            return iter(self._documents)

        def gen() -> Iterable[Chunk]:
            for ch in self._documents:
                src = ch.metadata.get("source") or ch.metadata.get("type")
                if src == source_filter:
                    yield ch

        return gen()

    def retrieve(
        self,
        query: str,
        top_k: int = 3,
        source_filter: Optional[str] = None,
    ) -> RetrievalResult:
        """
        Generic retrieval across all documents, optionally filtered by
        `source` / `type`.
        """
        q_vec = _build_vector(query)

        scored: List[Tuple[Chunk, float]] = []
        for ch in self._iter_filtered_documents(source_filter):
            d_vec = _build_vector(ch.content)
            score = _cosine_similarity(q_vec, d_vec)
            if score > 0:
                scored.append((ch, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        top = scored[:top_k]

        chunks = [c for c, _ in top]
        scores = [s for _, s in top]
        context_text = "\n\n".join(ch.content for ch in chunks)

        return RetrievalResult(chunks=chunks, scores=scores, context_text=context_text)

    def retrieve_both(
        self,
        query: str,
        req_k: int = 3,
        dbc_k: int = 5,
    ) -> Tuple[RetrievalResult, RetrievalResult]:
        """Retrieve requirement + DBC context (used by some code paths)."""
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
        """Retrieve requirement, DBC, and CAPL examples. req_metadata_filter/capl_pattern ignored in base impl."""
        req_res = self.retrieve(query, top_k=req_k, source_filter="requirement")
        dbc_res = self.retrieve(query, top_k=dbc_k, source_filter="dbc")
        capl_res = self.retrieve(query, top_k=capl_k, source_filter="capl")
        return req_res, dbc_res, capl_res

    # ---------------------------------------------------------------------
    # Index / stats
    # ---------------------------------------------------------------------

    def clear(self) -> None:
        """Clear all documents from the store and optionally clean cache directory."""
        self._documents = []
        self._document_count = 0
        # Clean cache directory if it exists and has files
        if self.cache_dir and os.path.isdir(self.cache_dir):
            try:
                for name in os.listdir(self.cache_dir):
                    path = os.path.join(self.cache_dir, name)
                    if os.path.isfile(path):
                        os.remove(path)
                    elif os.path.isdir(path):
                        shutil.rmtree(path, ignore_errors=True)
            except OSError:
                pass

    def build_index(self) -> None:
        """
        No‑op in this simple implementation.
        Hook kept for API compatibility.
        """

    def get_stats(self) -> Dict[str, int]:
        """Return basic statistics for UI display."""
        requirement_chunks = 0
        dbc_chunks = 0
        capl_scripts = 0
        for ch in self._documents:
            t = ch.metadata.get("source") or ch.metadata.get("type") or ""
            if t in ("requirement", "user_requirement"):
                requirement_chunks += 1
            elif t in ("dbc", "dbc_context"):
                dbc_chunks += 1
            elif t in ("capl", "capl_implementation"):
                capl_scripts += 1

        return {
            "total_documents": self._document_count,
            "requirement_chunks": requirement_chunks,
            "dbc_chunks": dbc_chunks,
            "capl_scripts": capl_scripts,
            "embedding_dimension": self._embedding_dim,
        }


# =============================================================================
# CONTEXT PROMPT BUILDERS
# =============================================================================


def _format_result_block(title: str, result: RetrievalResult) -> str:
    if not result.chunks:
        return f"=== {title} ===\n(No results)\n"

    lines = [f"=== {title} ==="]
    for i, ch in enumerate(result.chunks, start=1):
        lines.append(f"--- {title} #{i} ---")
        lines.append(ch.content)
        lines.append("")
    return "\n".join(lines)


def create_rag_context_prompt(
    requirement: str,
    results_tuple,
) -> str:
    """
    Build a unified context string from retrieval results.

    Used by:
    - app14 1.py (analysis + generation)
    - complete_rag_app.py (test case generation, CAPL, etc.)
    """
    parts: List[str] = [f"USER REQUIREMENT:\n{requirement}\n"]

    # results_tuple can be (req, dbc) or (req, dbc, capl)
    if len(results_tuple) >= 2:
        req_res, dbc_res = results_tuple[0], results_tuple[1]
        parts.append(_format_result_block("SIMILAR REQUIREMENTS", req_res))
        parts.append(_format_result_block("DBC CONTEXT", dbc_res))

    if len(results_tuple) >= 3 and results_tuple[2] is not None:
        capl_res = results_tuple[2]
        parts.append(_format_result_block("CAPL EXAMPLES", capl_res))

    return "\n".join(parts)


def build_enhanced_analysis_prompt(requirement: str, rag_context: str) -> str:
    """
    Wrap the requirement together with RAG context for analysis prompts.

    This is intentionally generic; the calling code (SIMULATION_ANALYSIS_PROMPT)
    adds more detailed instructions.
    """
    return f"""PRIMARY REQUIREMENT:
{requirement}

ADDITIONAL CONTEXT FROM VECTOR STORE:
{rag_context}
"""

