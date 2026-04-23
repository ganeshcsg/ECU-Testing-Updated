from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from api.deps import get_mod
from api.schemas import DBCParseResponse, DBCMessageOut, DBCSignalOut

router = APIRouter()


@router.post("/dbc/parse", response_model=DBCParseResponse)
async def parse_dbc(file: UploadFile = File(...), mod=Depends(get_mod)):
    if not file.filename.lower().endswith(".dbc"):
        raise HTTPException(400, "Only .dbc files accepted.")
    content = await file.read()
    try:
        dbc_ctx = mod.parse_dbc_file(content)
    except Exception as e:
        raise HTTPException(422, f"DBC parse error: {e}")

    messages_out = []
    for msg in dbc_ctx.messages:
        signals_out = [
            DBCSignalOut(
                name=sig.name,
                start_bit=sig.start_bit,
                length=sig.bit_length,
                byte_order=str(sig.byte_order),
                is_signed=bool(sig.is_signed),
                scale=float(sig.scale) if sig.scale is not None else 1.0,
                offset=float(sig.offset) if sig.offset is not None else 0.0,
                minimum=float(sig.minimum) if sig.minimum is not None else None,
                maximum=float(sig.maximum) if sig.maximum is not None else None,
                unit=sig.unit or "",
                receivers=list(sig.receivers or []),
            )
            for sig in msg.signals
        ]
        transmitter = getattr(msg, "transmitter", None) or ""
        senders = [transmitter] if transmitter else []
        messages_out.append(
            DBCMessageOut(
                frame_id=msg.frame_id,
                frame_id_hex=hex(msg.frame_id),
                name=msg.name,
                dlc=msg.dlc,
                cycle_time=getattr(msg, "cycle_time", None),
                senders=senders,
                signals=signals_out,
            )
        )

    return DBCParseResponse(
        node_names=[ecu.name for ecu in dbc_ctx.ecus],
        messages=messages_out,
        total_signals=sum(len(m.signals) for m in messages_out),
        summary=getattr(dbc_ctx, "raw_dbc_summary", ""),
    )
