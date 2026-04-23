from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from api.deps import get_mod
from api.schemas import RequirementsParseResponse

router = APIRouter()
_ACCEPTED = (".pdf", ".txt", ".xlsx", ".xls")


@router.post("/requirements/parse", response_model=RequirementsParseResponse)
async def parse_requirements(file: UploadFile = File(...), mod=Depends(get_mod)):
    if not any(file.filename.lower().endswith(ext) for ext in _ACCEPTED):
        raise HTTPException(400, f"Accepted: {', '.join(_ACCEPTED)}")
    content = await file.read()
    try:
        raw = mod.parse_requirement_file(content, file.filename)
        reqs = mod._parse_requirements_from_text(raw)
    except Exception as e:
        raise HTTPException(422, f"Parse error: {e}")
    return RequirementsParseResponse(requirements=reqs, total=len(reqs))
