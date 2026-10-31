"""FastAPI router for the quality report browser (mounted by portfolio serve)."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from . import quality as quality_mod
from .markets import MARKETS

router = APIRouter(prefix="/api/ohlcv/quality", tags=["ohlcv-quality"])

_REPORT_ID = re.compile(r"^\d{8}-\d{6}(?:-\d+)?$")


def _error(status: int, error: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": error, "message": message})


def _reports_root():
    return quality_mod.REPORTS_DIR


def _report_dir(report_id: str):
    if not _REPORT_ID.match(report_id):
        raise FileNotFoundError(
            f"quality report not found: {report_id}. Run: uv run ohlcv quality"
        )
    path = _reports_root() / report_id
    if not path.is_dir() or not (path / "manifest.json").is_file():
        raise FileNotFoundError(
            f"quality report not found: {report_id}. Run: uv run ohlcv quality"
        )
    return path


def _parse_days(days: int | None) -> int | None:
    if days is None:
        return None
    if days < 1:
        raise ValueError("days must be >= 1")
    return days


def _parse_expiry(text: str | None) -> date | None:
    if text is None or not str(text).strip():
        return None
    raw = str(text).strip()
    if len(raw) == 8 and raw.isdigit():
        return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
    return date.fromisoformat(raw)


def _parse_market(market: str | None) -> str | None:
    if market is None or not str(market).strip():
        return None
    key = str(market).strip().lower()
    if key not in MARKETS:
        known = ", ".join(sorted(MARKETS))
        raise ValueError(f"unknown market {market!r}; expected one of: {known}")
    return key


def _level(market: str | None, underlying: str | None, expiry: date | None) -> str:
    if expiry is not None:
        return "contract"
    if underlying:
        return "expiry"
    if market:
        return "underlying"
    return "market"


def _iso(value: date | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _row_payload(row: quality_mod.QualityRow) -> dict[str, Any]:
    return {
        "market": row.market,
        "underlying": row.underlying,
        "expiry": _iso(row.expiry),
        "token": row.token,
        "tradingsymbol": row.tradingsymbol,
        "instrument_type": row.instrument_type,
        "strike": row.strike,
        "session_days": row.session_days,
        "day_bars": row.day_bars,
        "day_gaps": row.day_gaps,
        "traded_days": row.traded_days,
        "minute_days": row.minute_days,
        "minute_pct": row.minute_pct,
        "no_day_data": row.no_day_data,
    }


def _report_payload(report_dir) -> dict[str, Any]:
    manifest = quality_mod.load_manifest(report_dir)
    markets: dict[str, Any] = {}
    for name, meta in (manifest.get("markets") or {}).items():
        markets[str(name)] = {
            "as_of": meta.get("as_of"),
            "day_files": meta.get("day_files"),
            "minute_files": meta.get("minute_files"),
            "contracts": meta.get("contracts"),
        }
    return {
        "id": report_dir.name,
        "generated_at": manifest.get("generated_at"),
        "schema_version": manifest.get("schema_version"),
        "markets": markets,
    }


def _require_schema(payload: dict[str, Any]) -> JSONResponse | None:
    version = payload.get("schema_version")
    try:
        version_int = int(version)
    except (TypeError, ValueError):
        version_int = -1
    if version_int != quality_mod.SCHEMA_VERSION:
        return _error(
            503,
            "quality_schema",
            f"quality report schema_version={version!r}; this UI reads version {quality_mod.SCHEMA_VERSION}",
        )
    return None


@router.get("/reports")
def quality_reports() -> dict[str, Any]:
    found = quality_mod.list_reports(_reports_root())
    reports = [_report_payload(path) for path in reversed(found)]
    return {"reports": reports}


@router.get("/reports/{report_id}", response_model=None)
def quality_report(report_id: str) -> dict[str, Any] | JSONResponse:
    try:
        path = _report_dir(report_id)
    except FileNotFoundError as exc:
        return _error(404, "quality_not_found", str(exc))
    payload = _report_payload(path)
    schema_error = _require_schema(payload)
    if schema_error is not None:
        return schema_error
    return payload


@router.get("/reports/{report_id}/summary", response_model=None)
def quality_summary(
    report_id: str,
    days: int | None = Query(None),
    market: str | None = Query(None),
    underlying: str | None = Query(None),
    expiry: str | None = Query(None),
) -> dict[str, Any] | JSONResponse:
    try:
        path = _report_dir(report_id)
        window = _parse_days(days)
        market_id = _parse_market(market)
        expiry_date = _parse_expiry(expiry)
    except FileNotFoundError as exc:
        return _error(404, "quality_not_found", str(exc))
    except ValueError as exc:
        return _error(400, "bad_request", str(exc))

    payload = _report_payload(path)
    schema_error = _require_schema(payload)
    if schema_error is not None:
        return schema_error

    name = (underlying or "").strip() or None
    level = _level(market_id, name, expiry_date)
    include = list(payload["markets"]) if level == "market" else None
    con = quality_mod.connect_report(path)
    try:
        rows = quality_mod.summarize(
            con,
            days=window,
            market=market_id,
            underlying=name,
            expiry=expiry_date,
            level=level,
            include_markets=include,
        )
    finally:
        con.close()
    return {
        "id": payload["id"],
        "generated_at": payload["generated_at"],
        "days": window,
        "level": level,
        "as_of": {key: meta.get("as_of") for key, meta in payload["markets"].items()},
        "rows": [_row_payload(row) for row in rows],
    }


@router.get("/reports/{report_id}/contract", response_model=None)
def quality_contract(
    report_id: str,
    token: int = Query(...),
    days: int | None = Query(None),
    market: str | None = Query(None),
) -> dict[str, Any] | JSONResponse:
    try:
        path = _report_dir(report_id)
        window = _parse_days(days)
        market_id = _parse_market(market)
    except FileNotFoundError as exc:
        return _error(404, "quality_not_found", str(exc))
    except ValueError as exc:
        return _error(400, "bad_request", str(exc))

    payload = _report_payload(path)
    schema_error = _require_schema(payload)
    if schema_error is not None:
        return schema_error

    con = quality_mod.connect_report(path)
    try:
        slices = quality_mod.contract_slices(
            con,
            days=window,
            market=market_id,
            token=token,
        )
    finally:
        con.close()
    if not slices:
        return _error(404, "quality_not_found", f"token {token} is not in this report")
    if len(slices) > 1:
        return _error(400, "bad_request", "market is required when the token exists in more than one market")
    item = slices[0]
    return {
        "id": payload["id"],
        "generated_at": payload["generated_at"],
        "days": window,
        "row": _row_payload(item.row),
        "gap_dates": [_iso(day) for day in item.gap_dates],
        "traded_without_minute": [_iso(day) for day in item.traded_without_minute],
    }


__all__ = ["router"]
