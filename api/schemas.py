from __future__ import annotations
from typing import Any, Dict, List, Optional
from pydantic import BaseModel


class DBCSignalOut(BaseModel):
    name: str
    start_bit: int
    length: int
    byte_order: str
    is_signed: bool
    scale: float
    offset: float
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    unit: str
    receivers: List[str]


class DBCMessageOut(BaseModel):
    frame_id: int
    frame_id_hex: str
    name: str
    dlc: int
    cycle_time: Optional[int] = None
    senders: List[str]
    signals: List[DBCSignalOut]


class DBCParseResponse(BaseModel):
    node_names: List[str]
    messages: List[DBCMessageOut]
    total_signals: int
    summary: str


class RequirementsParseResponse(BaseModel):
    requirements: List[str]
    total: int


class GenerateRequest(BaseModel):
    requirement: str
    dbc_b64: str  # base64-encoded DBC file bytes


class ArtifactOut(BaseModel):
    id: int
    requirement_text: Optional[str]
    test_cases: Optional[Any]
    capl_code: Optional[str]
    llm_model: Optional[str]
    status: Optional[str]
    generation_time_seconds: Optional[float]
    created_at: str


class FeedbackRequest(BaseModel):
    score: int   # 1–5
    text: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    ollama: bool
    qdrant: bool
    db: bool
    details: Dict[str, str] = {}
