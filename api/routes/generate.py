"""SSE streaming endpoint — streams live LLM tokens for test-case and CAPL generation."""

import asyncio
import base64
import dataclasses
import json
import threading
import time
from typing import AsyncGenerator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from api.deps import get_mod, get_pg_conn, get_rag_store
from api.schemas import GenerateRequest

router = APIRouter()

_DONE = object()  # sentinel that signals a streaming thread has finished


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


class _TokenQueue:
    """
    Fake Streamlit DeltaGenerator passed as stream_container to the core functions.

    Both generate_test_cases and _stream_or_invoke_chain (used by generate_simulation_capl)
    follow the same pattern:
        placeholder = stream_container.empty()
        for chunk in chain.stream(inputs):
            accumulated.append(chunk)
            placeholder.code("".join(accumulated), language=...)
        placeholder.empty()   # clear at end (no-op here)

    We intercept each .code() call and push the full accumulated text to an asyncio.Queue
    so the async pipeline generator can yield it as an SSE event.
    """

    def __init__(self, q: asyncio.Queue, loop: asyncio.AbstractEventLoop):
        self._q = q
        self._loop = loop

    # stream_container.empty() → returns self (acts as its own placeholder)
    def empty(self):
        return self

    def _push(self, text: str):
        if text:
            self._loop.call_soon_threadsafe(self._q.put_nowait, text)

    # All Streamlit write methods used in the codebase
    def code(self, text: str, **_):     self._push(text)
    def markdown(self, text: str, **_): self._push(text)
    def text(self, text: str, **_):     self._push(text)
    def write(self, *args, **_):
        for a in args:
            if isinstance(a, str):
                self._push(a)


async def _pipeline(
    payload: GenerateRequest, mod, rag_store, pg_conn
) -> AsyncGenerator[str, None]:

    # ── 1. Parse DBC ──────────────────────────────────────────────────────────
    yield _sse("status", {"step": "parsing_dbc", "message": "Parsing DBC file…"})
    try:
        dbc_bytes = base64.b64decode(payload.dbc_b64)
        dbc_ctx = await asyncio.to_thread(mod.parse_dbc_file, dbc_bytes)
    except Exception as e:
        yield _sse("error", {"step": "parsing_dbc", "message": str(e)})
        return

    t0 = time.monotonic()
    loop = asyncio.get_running_loop()

    # ── 2. Test Cases — live token streaming ──────────────────────────────────
    yield _sse("status", {"step": "test_cases", "message": "Generating test cases…"})

    tc_q: asyncio.Queue = asyncio.Queue()
    tc_result_box: list = [None]
    tc_exc_box: list = [None]

    def _run_tc():
        try:
            tc_result_box[0] = mod.generate_test_cases(
                payload.requirement, dbc_ctx, rag_store,
                None, None,                     # requirement_id, retrieved_log
                _TokenQueue(tc_q, loop),        # stream_container
            )
        except Exception as exc:
            tc_exc_box[0] = exc
        finally:
            loop.call_soon_threadsafe(tc_q.put_nowait, _DONE)

    threading.Thread(target=_run_tc, daemon=True).start()

    while True:
        item = await tc_q.get()
        if item is _DONE:
            break
        if isinstance(item, str) and item:
            yield _sse("tc_token", {"text": item})

    if tc_exc_box[0]:
        yield _sse("error", {"step": "test_cases", "message": str(tc_exc_box[0])})
        return

    tc_list = (tc_result_box[0] or {}).get("test_cases", [])
    yield _sse("test_cases_done", {"test_cases": tc_list})

    # ── 3. Requirement Analysis (no streaming — fast, no LLM token output) ───
    yield _sse("status", {"step": "analysis", "message": "Analysing requirement…"})
    try:
        analysis = await asyncio.to_thread(
            mod.analyze_requirement_for_simulation,
            payload.requirement, dbc_ctx, "Ollama", rag_store, None,
        )
    except Exception as e:
        yield _sse("error", {"step": "analysis", "message": str(e)})
        return

    try:
        analysis_dict = dataclasses.asdict(analysis)
    except Exception:
        analysis_dict = {}
    yield _sse("analysis_done", {"analysis": analysis_dict})

    # ── 4. CAPL Generation — live token streaming ─────────────────────────────
    yield _sse("status", {"step": "capl", "message": "Generating CAPL script…"})

    capl_q: asyncio.Queue = asyncio.Queue()
    capl_result_box: list = [None]
    capl_exc_box: list = [None]

    def _run_capl():
        try:
            capl_result_box[0] = mod.generate_simulation_capl(
                dbc_ctx, analysis, "Ollama", rag_store,
                None,                           # capl_examples
                _TokenQueue(capl_q, loop),      # stream_container
            )
        except Exception as exc:
            capl_exc_box[0] = exc
        finally:
            loop.call_soon_threadsafe(capl_q.put_nowait, _DONE)

    threading.Thread(target=_run_capl, daemon=True).start()

    while True:
        item = await capl_q.get()
        if item is _DONE:
            break
        if isinstance(item, str) and item:
            yield _sse("capl_token", {"text": item})

    if capl_exc_box[0]:
        yield _sse("error", {"step": "capl", "message": str(capl_exc_box[0])})
        return

    elapsed = round(time.monotonic() - t0, 2)
    capl = capl_result_box[0] or ""
    yield _sse("capl_done", {"capl_script": capl})

    # ── 5. Persist artifact ───────────────────────────────────────────────────
    artifact_id = None
    if pg_conn:
        try:
            from services.data_pipeline.pg_bridge import record_artifact  # type: ignore
            artifact_id = await asyncio.to_thread(
                record_artifact,
                pg_conn,
                payload.requirement,
                getattr(dbc_ctx, "raw_dbc_summary", ""),
                {"test_cases": tc_list},
                capl,
                None,
                elapsed,
            )
        except Exception as e:
            print(f"[API] record_artifact failed: {e}")

    yield _sse("done", {"artifact_id": artifact_id, "generation_time_seconds": elapsed})


@router.post("/generate/stream")
async def generate_stream(
    payload: GenerateRequest,
    mod=Depends(get_mod),
    rag_store=Depends(get_rag_store),
    pg_conn=Depends(get_pg_conn),
):
    return StreamingResponse(
        _pipeline(payload, mod, rag_store, pg_conn),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
