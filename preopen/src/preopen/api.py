"""FastAPI router for pre-open day browser (mounted by portfolio serve)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from . import read as read_mod

router = APIRouter(prefix="/api/preopen", tags=["preopen"])


@router.get("/days")
def preopen_days() -> dict[str, Any]:
    return read_mod.list_days_payload()


@router.get("/day/{ymd}", response_model=None)
def preopen_day(
    ymd: str,
    limit: int = Query(50, ge=1, le=500),
    min_value: float = Query(0.0, ge=0.0),
    exchange: str | None = Query(None, description="NSE or BSE"),
) -> dict[str, Any] | JSONResponse:
    try:
        return read_mod.load_day(
            ymd,
            limit=limit,
            min_value=min_value,
            exchange=exchange,
        )
    except FileNotFoundError as exc:
        return JSONResponse(
            status_code=404,
            content={
                "error": "preopen_not_found",
                "message": str(exc),
            },
        )
    except ValueError as exc:
        return JSONResponse(
            status_code=400,
            content={
                "error": "bad_request",
                "message": str(exc),
            },
        )


__all__ = ["router"]
