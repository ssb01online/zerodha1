"""JSON payloads for the portfolio API (shared by live and snapshot responses)."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from typing import Any

from .fingerprint import CompositionDiff
from .models import Holding, MfHolding, PortfolioView, Position
from .store import SnapshotMeta


def iso(value: datetime | date | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return value.isoformat()


def _pct(pnl: float, invested: float) -> float | None:
    if invested == 0:
        return None
    return pnl / invested * 100.0


def _holding_row(h: Holding, *, weight: float | None) -> dict[str, Any]:
    invested = h.average_price * h.owned_quantity
    current_value = h.last_price * h.owned_quantity
    day_change_value = h.day_change * h.owned_quantity
    return {
        **asdict(h),
        "invested": invested,
        "current_value": current_value,
        "pnl_pct": _pct(h.pnl, invested),
        "day_change_value": day_change_value,
        "weight": weight,
    }


def _mf_row(m: MfHolding) -> dict[str, Any]:
    invested = m.average_price * m.quantity
    current_value = m.last_price * m.quantity
    return {
        **asdict(m),
        "invested": invested,
        "current_value": current_value,
        "pnl_pct": _pct(m.pnl, invested),
    }


def _position_row(p: Position) -> dict[str, Any]:
    row = asdict(p)
    row["expiry"] = iso(p.expiry)
    row["is_open"] = p.quantity != 0
    return row


def _book_totals(
    invested: float,
    current_value: float,
    pnl: float,
    day_change: float,
) -> dict[str, Any]:
    return {
        "invested": invested,
        "current_value": current_value,
        "pnl": pnl,
        "pnl_pct": _pct(pnl, invested),
        "day_change": day_change,
    }


def _summary(
    holdings: list[dict[str, Any]],
    mf_holdings: list[dict[str, Any]],
    positions_net: list[dict[str, Any]],
) -> dict[str, Any]:
    eq_invested = sum(r["invested"] for r in holdings)
    eq_value = sum(r["current_value"] for r in holdings)
    eq_pnl = sum(r["pnl"] for r in holdings)
    eq_day = sum(r["day_change_value"] for r in holdings)

    mf_invested = sum(r["invested"] for r in mf_holdings)
    mf_value = sum(r["current_value"] for r in mf_holdings)
    mf_pnl = sum(r["pnl"] for r in mf_holdings)

    open_positions = [r for r in positions_net if r["is_open"]]
    pos_pnl = sum(r["pnl"] for r in open_positions)
    pos_m2m = sum(r["m2m"] for r in open_positions)

    combined_invested = eq_invested + mf_invested
    combined_value = eq_value + mf_value
    combined_pnl = eq_pnl + mf_pnl

    return {
        "equity": _book_totals(eq_invested, eq_value, eq_pnl, eq_day),
        "mf": _book_totals(mf_invested, mf_value, mf_pnl, 0.0),
        "combined": _book_totals(combined_invested, combined_value, combined_pnl, eq_day),
        "positions": {
            "open_count": len(open_positions),
            "pnl": pos_pnl,
            "m2m": pos_m2m,
        },
    }


def portfolio_payload(
    view: PortfolioView,
    *,
    source: str,
    snapshot: SnapshotMeta | None = None,
) -> dict[str, Any]:
    """Build the shared live/snapshot portfolio JSON shape."""
    total_value = sum(h.last_price * h.owned_quantity for h in view.holdings)

    holdings: list[dict[str, Any]] = []
    for h in view.holdings:
        current_value = h.last_price * h.owned_quantity
        weight = (current_value / total_value) if total_value else None
        holdings.append(_holding_row(h, weight=weight))

    mf_holdings = [_mf_row(m) for m in view.mf_holdings]

    net_rows = [_position_row(p) for p in view.positions_net]
    # Open (non-zero qty) first, then closed; stable within each group by symbol.
    net_rows.sort(key=lambda r: (not r["is_open"], r["exchange"], r["tradingsymbol"]))
    day_rows = [_position_row(p) for p in view.positions_day]
    day_rows.sort(key=lambda r: (not r["is_open"], r["exchange"], r["tradingsymbol"]))

    profile = view.raw.profile or {}
    payload: dict[str, Any] = {
        "as_of": iso(view.fetched_at),
        "source": source,
        "user": {
            "user_id": profile.get("user_id"),
            "user_name": profile.get("user_name"),
        },
        "summary": _summary(holdings, mf_holdings, net_rows),
        "holdings": holdings,
        "mf_holdings": mf_holdings,
        "positions": {"net": net_rows, "day": day_rows},
    }
    if snapshot is not None:
        payload["snapshot_id"] = snapshot.id
        payload["observed_at"] = iso(snapshot.observed_at)
        payload["effective_date"] = iso(snapshot.effective_date)
        payload["fingerprint_hash"] = snapshot.fingerprint_hash
    return payload


def diff_payload(diff: CompositionDiff) -> dict[str, Any]:
    return {
        "added": dict(diff.added),
        "removed": dict(diff.removed),
        "changed": {k: {"old": o, "new": n} for k, (o, n) in diff.changed.items()},
        "empty": diff.empty,
        "summary": diff.short_summary(),
    }


def snapshot_meta_payload(meta: SnapshotMeta) -> dict[str, Any]:
    return {
        "id": meta.id,
        "observed_at": iso(meta.observed_at),
        "effective_date": iso(meta.effective_date),
        "source": meta.source,
        "fingerprint_hash": meta.fingerprint_hash,
        "counts": meta.counts,
    }


__all__ = [
    "diff_payload",
    "iso",
    "portfolio_payload",
    "snapshot_meta_payload",
]
