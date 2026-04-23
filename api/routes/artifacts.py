from typing import List
from fastapi import APIRouter, Depends, HTTPException
from api.deps import get_pg_conn
from api.schemas import ArtifactOut, FeedbackRequest

router = APIRouter()

_SELECT = """
    SELECT
        ga.id,
        COALESCE(r.title, r.description, 'Unknown') AS requirement_text,
        ga.generated_test_cases,
        ga.generated_capl_code,
        ga.generated_python_code,
        ga.llm_model,
        ga.status,
        ga.generation_time_seconds,
        ga.created_at
    FROM generated_artifacts ga
    LEFT JOIN requirements r ON r.id = ga.requirement_id
"""


def _row(row) -> ArtifactOut:
    id_, req, tc, capl, py, model, status, gen_time, created = row
    return ArtifactOut(
        id=id_,
        requirement_text=req,
        test_cases=tc,
        capl_code=capl,
        llm_model=model,
        status=status,
        generation_time_seconds=float(gen_time) if gen_time else None,
        created_at=str(created),
    )


@router.get("/artifacts", response_model=List[ArtifactOut])
async def list_artifacts(limit: int = 50, offset: int = 0, pg_conn=Depends(get_pg_conn)):
    if not pg_conn:
        return []
    try:
        cur = pg_conn.cursor()
        cur.execute(_SELECT + "ORDER BY ga.created_at DESC LIMIT %s OFFSET %s", (limit, offset))
        return [_row(r) for r in cur.fetchall()]
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/artifacts/{artifact_id}", response_model=ArtifactOut)
async def get_artifact(artifact_id: int, pg_conn=Depends(get_pg_conn)):
    if not pg_conn:
        raise HTTPException(503, "Database not configured.")
    try:
        cur = pg_conn.cursor()
        cur.execute(_SELECT + "WHERE ga.id = %s", (artifact_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Artifact not found.")
        return _row(row)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/artifacts/{artifact_id}/feedback")
async def submit_feedback(artifact_id: int, body: FeedbackRequest, pg_conn=Depends(get_pg_conn)):
    if not pg_conn:
        raise HTTPException(503, "Database not configured.")
    if not 1 <= body.score <= 5:
        raise HTTPException(400, "score must be 1–5.")
    try:
        cur = pg_conn.cursor()
        cur.execute(
            "INSERT INTO generation_feedback (generated_artifact_id, feedback_score, feedback_text) VALUES (%s,%s,%s)",
            (artifact_id, body.score, body.text),
        )
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))
