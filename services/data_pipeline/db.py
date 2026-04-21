"""
Database connection management.

Reads DATABASE_URL from the environment. Provides a psycopg2 connection
factory and a lightweight context manager for short-lived operations.

Supported URL formats:
    postgresql+psycopg2://user:pass@host:5432/dbname
    postgresql://user:pass@host:5432/dbname
"""
from __future__ import annotations

import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
except ImportError:
    pass

try:
    import psycopg2
    import psycopg2.extras
except ImportError:  # pragma: no cover
    psycopg2 = None  # type: ignore


_DSN_CACHE: dict[str, str] = {}


def _url_to_dsn(url: str) -> str:
    """Convert a SQLAlchemy-style URL to a psycopg2 DSN string."""
    if url in _DSN_CACHE:
        return _DSN_CACHE[url]
    # Strip driver prefix
    cleaned = re.sub(r"^postgresql\+\w+://", "postgresql://", url)
    _DSN_CACHE[url] = cleaned
    return cleaned


def get_database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. "
            "Example: postgresql+psycopg2://ecu_user:secret@localhost:5432/ecu_testing"
        )
    return url


def get_connection() -> "psycopg2.connection":
    if psycopg2 is None:
        raise ImportError("psycopg2 is required. Install with: pip install psycopg2-binary")
    import json as _json
    dsn = _url_to_dsn(get_database_url())
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    # Ensure JSONB columns are returned as Python dicts, not strings.
    psycopg2.extras.register_json(conn, globally=False, loads=_json.loads)
    return conn


@contextmanager
def managed_connection() -> Generator["psycopg2.connection", None, None]:
    """Yield a connection that auto-commits on clean exit and rolls back on error."""
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
