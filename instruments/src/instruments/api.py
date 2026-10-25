"""FastAPI router for the instruments browser (mounted by portfolio serve)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from . import query
from .cache import instrument_to_json

router = APIRouter(prefix="/api/instruments", tags=["instruments"])


@router.get("/meta")
def instruments_meta() -> dict[str, Any]:
    return query.meta()


@router.get("/search")
def instruments_search(
    q: str = Query("", description="Substring match on tradingsymbol and name."),
    exchange: str | None = Query(None, description="NSE, BSE, or NFO."),
    instrument_type: str | None = Query(
        None, description="EQ, FUT, CE, or PE."
    ),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    return query.search(
        q=q,
        exchange=exchange,
        instrument_type=instrument_type,
        limit=limit,
        offset=offset,
    )


@router.get("/by-token/{token}", response_model=None)
def instruments_by_token(token: int) -> dict[str, Any] | JSONResponse:
    inst = query.get(token)
    if inst is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": "instrument_not_found",
                "message": f"No instrument for token {token}",
            },
        )
    return instrument_to_json(inst)


@router.get("/nfo/underlyings")
def nfo_underlyings(
    q: str = Query("", description="Substring filter on underlying name."),
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    return query.underlyings(q=q, limit=limit)


@router.get("/nfo/expiries")
def nfo_expiries(
    underlying: str = Query(..., min_length=1, description="NFO underlying name."),
) -> dict[str, Any]:
    return query.expiries(underlying)


@router.get("/nfo/chain")
def nfo_chain(
    underlying: str = Query(..., min_length=1),
    expiry: str = Query(..., min_length=10, description="Expiry YYYY-MM-DD."),
) -> dict[str, Any]:
    return query.chain(underlying, expiry)


__all__ = ["router"]
