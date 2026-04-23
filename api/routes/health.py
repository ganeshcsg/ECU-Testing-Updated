import os
import requests
from fastapi import APIRouter, Request
from api.schemas import HealthResponse

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health_check(request: Request):
    details: dict[str, str] = {}

    # Ollama
    ollama_ok = False
    try:
        base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        r = requests.get(f"{base_url}/api/tags", timeout=5)
        ollama_ok = r.status_code == 200
        details["ollama_url"] = base_url
    except Exception as e:
        details["ollama_error"] = str(e)

    # Qdrant
    qdrant_ok = False
    try:
        qdrant_ok = request.app.state.rag_store is not None
        details["qdrant"] = "ready" if qdrant_ok else "not initialised"
    except Exception as e:
        details["qdrant_error"] = str(e)

    # PostgreSQL
    db_ok = False
    try:
        pg = request.app.state.pg_conn
        if pg:
            pg.cursor().execute("SELECT 1")
            db_ok = True
        details["db"] = "connected" if db_ok else "not configured"
    except Exception as e:
        details["db_error"] = str(e)

    return HealthResponse(
        status="ok" if (qdrant_ok and ollama_ok) else "degraded",
        ollama=ollama_ok,
        qdrant=qdrant_ok,
        db=db_ok,
        details=details,
    )
