"""FastAPI entry point — imports core logic from complete_rag_appV1.0.py via importlib."""

import asyncio
import importlib.util
import pathlib
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from api.routes import artifacts, dbc, generate, health, requirements

APP_DIR = pathlib.Path(__file__).parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

# Load .env so DATABASE_URL and other variables are available
load_dotenv(APP_DIR / ".env")


def _load_rag_app():
    spec = importlib.util.spec_from_file_location(
        "rag_app", str(APP_DIR / "complete_rag_appV1.0.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load core module
    mod = await asyncio.to_thread(_load_rag_app)
    app.state.mod = mod

    # Init RAG vector store
    from rag_vector_store_qdrant import ExtendedRAGVectorStore  # type: ignore
    rag_store = ExtendedRAGVectorStore(
        path=str(APP_DIR / "qdrant_data"),
        collection_name="rag_ecu",
    )
    loaded = await asyncio.to_thread(mod.load_data_v1_into_rag, rag_store)
    print(f"[API] RAG ready — {loaded} chunks loaded.")
    app.state.rag_store = rag_store

    # Init PostgreSQL (optional)
    pg_conn = None
    try:
        from services.data_pipeline.pg_bridge import init_for_app  # type: ignore
        pg_conn, dv_id = await asyncio.to_thread(init_for_app, APP_DIR)
        if pg_conn:
            rag_store.attach_db(pg_conn, dv_id)
            print("[API] PostgreSQL connected.")
        else:
            print("[API] PostgreSQL not configured.")
    except Exception as e:
        print(f"[API] PostgreSQL init skipped: {e}")
    app.state.pg_conn = pg_conn

    yield

    if pg_conn:
        try:
            pg_conn.close()
        except Exception:
            pass


app = FastAPI(
    title="ECU Testing AI API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router, prefix="/api", tags=["Health"])
app.include_router(dbc.router, prefix="/api", tags=["DBC"])
app.include_router(requirements.router, prefix="/api", tags=["Requirements"])
app.include_router(generate.router, prefix="/api", tags=["Generate"])
app.include_router(artifacts.router, prefix="/api", tags=["Artifacts"])

# Serve built React app (production mode — run `npm run build` in frontend/ first)
DIST = APP_DIR / "frontend" / "dist"
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="static-assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(full_path: str):
        return FileResponse(DIST / "index.html")
else:
    # Simple single-file HTML UI — no npm / no build step required
    UI_DIR = APP_DIR / "ui"
    if UI_DIR.exists():
        @app.get("/", include_in_schema=False)
        @app.get("/history", include_in_schema=False)
        async def serve_ui():
            return FileResponse(UI_DIR / "index.html")
