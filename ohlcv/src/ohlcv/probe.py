"""Read-only Kite historical / quote probe (Phase 0).

Measures range limits, OI, sparsity, listing dates, quote semantics, and latency.
Writes a JSON report under data/ohlcv/nfo/_probe/. Does not store candles.
"""

from __future__ import annotations

import json
import statistics
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from kiteconnect import KiteConnect
from kiteconnect.exceptions import DataException, NetworkException, TokenException

from instruments import Instrument, ensure_cache, iter_instruments
from kitetools import auth

from .paths import PROBE_DIR, ensure_dir

HIST_MIN_INTERVAL = 0.34  # ~3 req/s
QUOTE_MIN_INTERVAL = 1.0  # 1 req/s
RANGE_SPANS_DAYS = [30, 60, 61, 100, 101, 200, 400, 2000, 2001]
NEAREST_EXPIRY_FALLBACK = date(2026, 10, 27)


@dataclass(frozen=True)
class Selected:
    role: str
    instrument_token: int
    tradingsymbol: str
    name: str
    expiry: str | None
    instrument_type: str
    strike: float
    exchange: str


@dataclass
class TimedCall:
    kind: str
    elapsed_s: float
    ok: bool
    rows: int = 0
    error: str | None = None


class PacedKite:
    """Thin wrapper that paces historical and quote calls."""

    def __init__(self, kite: KiteConnect) -> None:
        self.kite = kite
        self._last_hist = 0.0
        self._last_quote = 0.0
        self.calls: list[TimedCall] = []

    def _wait(self, last: float, minimum: float) -> None:
        gap = minimum - (time.monotonic() - last)
        if gap > 0:
            time.sleep(gap)

    def historical(
        self,
        instrument_token: int,
        from_date: datetime | date | str,
        to_date: datetime | date | str,
        interval: str,
        *,
        oi: bool = False,
        continuous: bool = False,
    ) -> tuple[list[dict[str, Any]], TimedCall]:
        self._wait(self._last_hist, HIST_MIN_INTERVAL)
        t0 = time.monotonic()
        try:
            rows = self.kite.historical_data(
                instrument_token,
                from_date,
                to_date,
                interval,
                continuous=continuous,
                oi=oi,
            )
            elapsed = time.monotonic() - t0
            self._last_hist = time.monotonic()
            call = TimedCall("historical", elapsed, True, rows=len(rows))
            self.calls.append(call)
            return rows, call
        except Exception as exc:
            elapsed = time.monotonic() - t0
            self._last_hist = time.monotonic()
            call = TimedCall("historical", elapsed, False, error=f"{type(exc).__name__}: {exc}")
            self.calls.append(call)
            if isinstance(exc, TokenException):
                raise
            return [], call

    def quote(self, instruments: list[str]) -> tuple[dict[str, Any], TimedCall]:
        self._wait(self._last_quote, QUOTE_MIN_INTERVAL)
        t0 = time.monotonic()
        try:
            data = self.kite.quote(instruments)
            elapsed = time.monotonic() - t0
            self._last_quote = time.monotonic()
            call = TimedCall("quote", elapsed, True, rows=len(data))
            self.calls.append(call)
            return data, call
        except Exception as exc:
            elapsed = time.monotonic() - t0
            self._last_quote = time.monotonic()
            call = TimedCall("quote", elapsed, False, error=f"{type(exc).__name__}: {exc}")
            self.calls.append(call)
            if isinstance(exc, TokenException):
                raise
            return {}, call

    def ltp(self, instruments: list[str]) -> tuple[dict[str, Any], TimedCall]:
        self._wait(self._last_quote, QUOTE_MIN_INTERVAL)
        t0 = time.monotonic()
        try:
            data = self.kite.ltp(instruments)
            elapsed = time.monotonic() - t0
            self._last_quote = time.monotonic()
            call = TimedCall("ltp", elapsed, True, rows=len(data))
            self.calls.append(call)
            return data, call
        except Exception as exc:
            elapsed = time.monotonic() - t0
            self._last_quote = time.monotonic()
            call = TimedCall("ltp", elapsed, False, error=f"{type(exc).__name__}: {exc}")
            self.calls.append(call)
            if isinstance(exc, TokenException):
                raise
            return {}, call


def _today_ist() -> date:
    return datetime.now(auth.IST).date()


def _to_selected(role: str, inst: Instrument) -> Selected:
    return Selected(
        role=role,
        instrument_token=inst.instrument_token,
        tradingsymbol=inst.tradingsymbol,
        name=inst.name,
        expiry=inst.expiry.isoformat() if inst.expiry else None,
        instrument_type=inst.instrument_type,
        strike=inst.strike,
        exchange=inst.exchange,
    )


def _nfo_rows() -> list[Instrument]:
    ensure_cache()
    return [r for r in iter_instruments() if r.exchange == "NFO"]


def _find_index_nifty50() -> Instrument | None:
    ensure_cache()
    for r in iter_instruments():
        if r.exchange == "NSE" and r.tradingsymbol == "NIFTY 50" and r.segment == "INDICES":
            return r
    return None


def _nearest_fut(rows: list[Instrument], name: str) -> Instrument | None:
    futs = [
        r
        for r in rows
        if r.name == name and r.instrument_type == "FUT" and r.expiry is not None
    ]
    if not futs:
        return None
    return sorted(futs, key=lambda r: r.expiry)[0]  # type: ignore[arg-type, return-value]


def _fut_by_expiry(rows: list[Instrument], name: str, expiry: date) -> Instrument | None:
    for r in rows:
        if r.name == name and r.instrument_type == "FUT" and r.expiry == expiry:
            return r
    return None


def _ce_atm(rows: list[Instrument], name: str, expiry: date, spot: float) -> Instrument | None:
    ces = [
        r
        for r in rows
        if r.name == name and r.instrument_type == "CE" and r.expiry == expiry and r.strike > 0
    ]
    if not ces:
        return None
    if spot <= 0:
        # Fall back to median strike when spot unknown.
        ces_sorted = sorted(ces, key=lambda r: r.strike)
        return ces_sorted[len(ces_sorted) // 2]
    return min(ces, key=lambda r: abs(r.strike - spot))


def _ce_far_otm(rows: list[Instrument], name: str, expiry: date) -> Instrument | None:
    ces = [
        r
        for r in rows
        if r.name == name and r.instrument_type == "CE" and r.expiry == expiry and r.strike > 0
    ]
    if not ces:
        return None
    return max(ces, key=lambda r: r.strike)


def select_contracts(
    paced: PacedKite,
    *,
    token_override: int | None = None,
) -> dict[str, Selected]:
    rows = _nfo_rows()
    out: dict[str, Selected] = {}

    if token_override is not None:
        ensure_cache()
        for r in iter_instruments():
            if r.instrument_token == token_override:
                out["override"] = _to_selected("override", r)
                break
        if "override" not in out:
            out["override"] = Selected(
                role="override",
                instrument_token=token_override,
                tradingsymbol=f"TOKEN_{token_override}",
                name="",
                expiry=None,
                instrument_type="",
                strike=0.0,
                exchange="",
            )

    idx = _find_index_nifty50()
    if idx is not None:
        out["index"] = _to_selected("index", idx)

    nifty_fut = _nearest_fut(rows, "NIFTY")
    if nifty_fut is not None:
        out["idx_fut"] = _to_selected("idx_fut", nifty_fut)

    rel_fut = _nearest_fut(rows, "RELIANCE")
    if rel_fut is not None:
        out["stk_fut"] = _to_selected("stk_fut", rel_fut)

    # Spot for ATM: prefer LTP of nearest NIFTY FUT, else dump last_price.
    spot = 0.0
    if nifty_fut is not None:
        ltp_data, _ = paced.ltp([f"NFO:{nifty_fut.tradingsymbol}"])
        key = f"NFO:{nifty_fut.tradingsymbol}"
        if key in ltp_data:
            spot = float(ltp_data[key].get("last_price") or 0)
        if spot <= 0:
            spot = float(nifty_fut.last_price or 0)

    expiry_near = nifty_fut.expiry if nifty_fut and nifty_fut.expiry else NEAREST_EXPIRY_FALLBACK

    atm = _ce_atm(rows, "NIFTY", expiry_near, spot)
    if atm is not None:
        out["idx_atm"] = _to_selected("idx_atm", atm)

    far_otm = _ce_far_otm(rows, "RELIANCE", expiry_near)
    if far_otm is not None:
        out["stk_far_otm"] = _to_selected("stk_far_otm", far_otm)

    far_exp = date(2026, 12, 29)
    far_fut = _fut_by_expiry(rows, "RELIANCE", far_exp)
    if far_fut is None:
        # Fall back to farthest RELIANCE FUT.
        futs = [
            r
            for r in rows
            if r.name == "RELIANCE" and r.instrument_type == "FUT" and r.expiry is not None
        ]
        if futs:
            far_fut = sorted(futs, key=lambda r: r.expiry)[-1]  # type: ignore[arg-type]
    if far_fut is not None:
        out["stk_far_expiry"] = _to_selected("stk_far_expiry", far_fut)

    out["_meta_spot"] = Selected(
        role="_meta_spot",
        instrument_token=0,
        tradingsymbol=f"spot={spot}",
        name="NIFTY",
        expiry=expiry_near.isoformat(),
        instrument_type="META",
        strike=spot,
        exchange="",
    )
    return out


def _candle_date(c: dict[str, Any]) -> date | None:
    d = c.get("date")
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    try:
        return datetime.fromisoformat(str(d)).date()
    except ValueError:
        return None


def _session_stats(candles: list[dict[str, Any]]) -> dict[str, Any]:
    by_day: dict[str, list[dict[str, Any]]] = {}
    for c in candles:
        d = _candle_date(c)
        if d is None:
            continue
        by_day.setdefault(d.isoformat(), []).append(c)

    days_out: list[dict[str, Any]] = []
    gap_minutes = 0
    zero_vol_minutes = 0
    for day, rows in sorted(by_day.items()):
        rows_sorted = sorted(rows, key=lambda r: r["date"])
        vols = [int(r.get("volume") or 0) for r in rows_sorted]
        zero_vol_minutes += sum(1 for v in vols if v == 0)
        # Gap detection: consecutive timestamps more than 60s apart within session.
        for a, b in zip(rows_sorted, rows_sorted[1:]):
            da, db = a["date"], b["date"]
            if isinstance(da, datetime) and isinstance(db, datetime):
                delta = (db - da).total_seconds()
                if delta > 60:
                    gap_minutes += int(delta // 60) - 1
        first_ts = rows_sorted[0]["date"]
        last_ts = rows_sorted[-1]["date"]
        days_out.append(
            {
                "day": day,
                "rows": len(rows_sorted),
                "vs_375": len(rows_sorted) - 375,
                "first": str(first_ts),
                "last": str(last_ts),
                "volume_sum": sum(vols),
                "zero_vol_rows": sum(1 for v in vols if v == 0),
            }
        )
    return {
        "days": days_out,
        "inferred_gaps_omit_empty": gap_minutes > 0 or any(d["rows"] < 375 for d in days_out),
        "gap_minutes_estimate": gap_minutes,
        "zero_vol_minutes_total": zero_vol_minutes,
        "note": (
            "If days have <375 rows and few zero-vol rows, non-traded minutes are omitted. "
            "If rows~375 with many zero-vol, empty minutes are returned."
        ),
    }


def check_max_range(paced: PacedKite, token: int, interval: str) -> dict[str, Any]:
    end = _today_ist() - timedelta(days=1)
    results: list[dict[str, Any]] = []
    max_ok: int | None = None
    first_fail: dict[str, Any] | None = None
    for span in RANGE_SPANS_DAYS:
        start = end - timedelta(days=span - 1)
        from_s = f"{start.isoformat()} 09:15:00"
        to_s = f"{end.isoformat()} 15:30:00"
        rows, call = paced.historical(token, from_s, to_s, interval, oi=False)
        entry = {
            "span_days": span,
            "from": from_s,
            "to": to_s,
            "ok": call.ok,
            "rows": call.rows,
            "elapsed_s": round(call.elapsed_s, 3),
            "error": call.error,
        }
        results.append(entry)
        if call.ok:
            max_ok = span
        else:
            # InputException = range too large; stop ascending.
            if call.error and "InputException" in call.error:
                first_fail = entry
                break
            # Other errors: record and continue (don't treat as limit).
            if first_fail is None:
                first_fail = entry
    return {
        "interval": interval,
        "token": token,
        "max_ok_span_days": max_ok,
        "first_fail": first_fail,
        "trials": results,
    }


def check_oi(paced: PacedKite, sel: Selected) -> dict[str, Any]:
    end = _today_ist() - timedelta(days=1)
    start = end - timedelta(days=4)
    out: dict[str, Any] = {"token": sel.instrument_token, "symbol": sel.tradingsymbol}
    for interval in ("minute", "day"):
        from_s = f"{start.isoformat()} 09:15:00"
        to_s = f"{end.isoformat()} 15:30:00"
        rows, call = paced.historical(sel.instrument_token, from_s, to_s, interval, oi=True)
        oi_vals = [r.get("oi") for r in rows if "oi" in r]
        out[interval] = {
            "ok": call.ok,
            "error": call.error,
            "rows": call.rows,
            "has_oi_field": bool(oi_vals),
            "oi_non_null": sum(1 for v in oi_vals if v is not None),
            "oi_sample": oi_vals[:5],
            "oi_unique": len({v for v in oi_vals if v is not None}),
            "fields_on_first": list(rows[0].keys()) if rows else [],
        }
    return out


def check_sparsity(paced: PacedKite, sel: Selected) -> dict[str, Any]:
    end = _today_ist() - timedelta(days=1)
    start = end - timedelta(days=10)  # cover ~5 sessions across weekends
    from_s = f"{start.isoformat()} 09:15:00"
    to_s = f"{end.isoformat()} 15:30:00"
    rows, call = paced.historical(sel.instrument_token, from_s, to_s, "minute", oi=True)
    stats = _session_stats(rows) if call.ok else {}
    return {
        "token": sel.instrument_token,
        "symbol": sel.tradingsymbol,
        "ok": call.ok,
        "error": call.error,
        "rows": call.rows,
        "stats": stats,
    }


def check_listing(paced: PacedKite, sel: Selected) -> dict[str, Any]:
    end = _today_ist() - timedelta(days=1)
    start = end - timedelta(days=119)
    from_s = start.isoformat()
    to_s = end.isoformat()
    rows, call = paced.historical(sel.instrument_token, from_s, to_s, "day", oi=True)
    first = _candle_date(rows[0]) if rows else None
    last = _candle_date(rows[-1]) if rows else None
    # Pre-listing window: 30 days before first candle (or far past if empty).
    pre_result: dict[str, Any]
    if first is not None:
        pre_end = first - timedelta(days=1)
        pre_start = pre_end - timedelta(days=29)
        pre_rows, pre_call = paced.historical(
            sel.instrument_token,
            pre_start.isoformat(),
            pre_end.isoformat(),
            "day",
            oi=True,
        )
        pre_result = {
            "from": pre_start.isoformat(),
            "to": pre_end.isoformat(),
            "ok": pre_call.ok,
            "rows": pre_call.rows,
            "error": pre_call.error,
            "empty_not_error": pre_call.ok and pre_call.rows == 0,
        }
    else:
        pre_result = {"skipped": True, "reason": "no candles in 120d window"}
    return {
        "token": sel.instrument_token,
        "symbol": sel.tradingsymbol,
        "ok": call.ok,
        "error": call.error,
        "rows": call.rows,
        "first_candle": first.isoformat() if first else None,
        "last_candle": last.isoformat() if last else None,
        "pre_listing": pre_result,
    }


def check_day_vs_minute(paced: PacedKite, sel: Selected) -> dict[str, Any]:
    # Pick a recent weekday: walk back from yesterday until we get day candles.
    day = _today_ist() - timedelta(days=1)
    day_rows: list[dict[str, Any]] = []
    day_call: TimedCall | None = None
    for _ in range(10):
        day_rows, day_call = paced.historical(
            sel.instrument_token,
            day.isoformat(),
            day.isoformat(),
            "day",
            oi=True,
        )
        if day_call.ok and day_rows:
            break
        day -= timedelta(days=1)
    else:
        return {
            "token": sel.instrument_token,
            "symbol": sel.tradingsymbol,
            "ok": False,
            "error": "no day candle in last 10 calendar days",
        }

    from_s = f"{day.isoformat()} 09:15:00"
    to_s = f"{day.isoformat()} 15:30:00"
    min_rows, min_call = paced.historical(sel.instrument_token, from_s, to_s, "minute", oi=True)
    day_vol = int(day_rows[0].get("volume") or 0) if day_rows else None
    min_vol = sum(int(r.get("volume") or 0) for r in min_rows)
    day_oi = day_rows[0].get("oi") if day_rows else None
    last_min_oi = min_rows[-1].get("oi") if min_rows else None
    return {
        "token": sel.instrument_token,
        "symbol": sel.tradingsymbol,
        "trade_date": day.isoformat(),
        "day_ok": day_call.ok if day_call else False,
        "minute_ok": min_call.ok,
        "day_volume": day_vol,
        "minute_volume_sum": min_vol,
        "volume_match": day_vol == min_vol if day_vol is not None else None,
        "day_oi": day_oi,
        "last_minute_oi": last_min_oi,
        "oi_match": day_oi == last_min_oi if day_oi is not None else None,
        "minute_rows": min_call.rows,
        "day_error": day_call.error if day_call else None,
        "minute_error": min_call.error,
    }


def check_timestamps(paced: PacedKite, sel: Selected) -> dict[str, Any]:
    day = _today_ist() - timedelta(days=1)
    for _ in range(10):
        from_s = f"{day.isoformat()} 09:15:00"
        to_s = f"{day.isoformat()} 15:30:00"
        rows, call = paced.historical(sel.instrument_token, from_s, to_s, "minute", oi=False)
        if call.ok and rows:
            first = rows[0]["date"]
            last = rows[-1]["date"]
            tzinfo = getattr(first, "tzinfo", None)
            return {
                "token": sel.instrument_token,
                "symbol": sel.tradingsymbol,
                "trade_date": day.isoformat(),
                "ok": True,
                "first": str(first),
                "last": str(last),
                "tzinfo": str(tzinfo),
                "utc_offset_hours": (
                    tzinfo.utcoffset(first).total_seconds() / 3600
                    if tzinfo and tzinfo.utcoffset(first) is not None
                    else None
                ),
                "first_time_ist": first.strftime("%H:%M") if isinstance(first, datetime) else None,
                "last_time_ist": last.strftime("%H:%M") if isinstance(last, datetime) else None,
            }
        day -= timedelta(days=1)
    return {
        "token": sel.instrument_token,
        "symbol": sel.tradingsymbol,
        "ok": False,
        "error": "no minute candles in last 10 calendar days",
    }


def check_quotes(paced: PacedKite, rows: list[Instrument]) -> dict[str, Any]:
    # Nearest expiry across NFO; take up to 500 tradingsymbols.
    with_exp = [r for r in rows if r.expiry is not None]
    if not with_exp:
        return {"ok": False, "error": "no NFO rows with expiry"}
    nearest = min(r.expiry for r in with_exp)  # type: ignore[type-var]
    pool = [r for r in with_exp if r.expiry == nearest]
    # Prefer diverse underlyings: FUT first, then CE/PE.
    pool_sorted = sorted(
        pool,
        key=lambda r: (0 if r.instrument_type == "FUT" else 1, r.name, r.strike),
    )
    batch = pool_sorted[:500]
    keys = [f"NFO:{r.tradingsymbol}" for r in batch]
    data, call = paced.quote(keys)

    samples: list[dict[str, Any]] = []
    for key, q in list(data.items())[:5]:
        ohlc = q.get("ohlc") or {}
        samples.append(
            {
                "key": key,
                "last_price": q.get("last_price"),
                "ohlc_close": ohlc.get("close"),
                "ohlc_open": ohlc.get("open"),
                "volume": q.get("volume"),
                "average_price": q.get("average_price"),
                "oi": q.get("oi"),
                "open_interest": q.get("open_interest"),
                "timestamp": str(q.get("timestamp")),
                "last_trade_time": str(q.get("last_trade_time")),
            }
        )

    # Field presence across all returned.
    field_counts = {
        "last_trade_time": 0,
        "timestamp": 0,
        "volume": 0,
        "oi": 0,
        "average_price": 0,
        "ohlc.close": 0,
        "last_price": 0,
    }
    close_vs_ltp: list[dict[str, Any]] = []
    for key, q in data.items():
        if q.get("last_trade_time") is not None:
            field_counts["last_trade_time"] += 1
        if q.get("timestamp") is not None:
            field_counts["timestamp"] += 1
        if q.get("volume") is not None:
            field_counts["volume"] += 1
        if q.get("oi") is not None:
            field_counts["oi"] += 1
        if q.get("average_price") is not None:
            field_counts["average_price"] += 1
        ohlc = q.get("ohlc") or {}
        if ohlc.get("close") is not None:
            field_counts["ohlc.close"] += 1
        if q.get("last_price") is not None:
            field_counts["last_price"] += 1
        if len(close_vs_ltp) < 20:
            close_vs_ltp.append(
                {
                    "key": key,
                    "ohlc_close": ohlc.get("close"),
                    "last_price": q.get("last_price"),
                    "equal": ohlc.get("close") == q.get("last_price"),
                }
            )

    today = _today_ist()
    return {
        "ok": call.ok,
        "error": call.error,
        "requested": len(keys),
        "returned": call.rows,
        "elapsed_s": round(call.elapsed_s, 3),
        "nearest_expiry": nearest.isoformat(),
        "run_date_ist": today.isoformat(),
        "weekday": today.strftime("%A"),
        "is_weekend": today.weekday() >= 5,
        "field_counts": field_counts,
        "samples": samples,
        "close_vs_ltp_sample": close_vs_ltp,
        "note": (
            "ohlc.close is previous day's close per Kite docs; after market close, "
            "session close should come from last_price. Weekend run may show last session values."
        ),
    }


def build_watch_list(rows: list[Instrument], selected: dict[str, Selected]) -> list[dict[str, Any]]:
    """~10 tokens from nearest expiry (NIFTY/RELIANCE FUT + ATM + far OTM)."""
    expiry_s = selected.get("idx_fut")
    expiry = date.fromisoformat(expiry_s.expiry) if expiry_s and expiry_s.expiry else NEAREST_EXPIRY_FALLBACK
    watch: list[dict[str, Any]] = []
    seen: set[int] = set()

    def add(inst: Instrument | None, note: str) -> None:
        if inst is None or inst.instrument_token in seen:
            return
        seen.add(inst.instrument_token)
        watch.append(
            {
                "instrument_token": inst.instrument_token,
                "tradingsymbol": inst.tradingsymbol,
                "name": inst.name,
                "expiry": inst.expiry.isoformat() if inst.expiry else None,
                "instrument_type": inst.instrument_type,
                "strike": inst.strike,
                "note": note,
            }
        )

    for role in ("idx_fut", "stk_fut", "idx_atm", "stk_far_otm"):
        sel = selected.get(role)
        if sel and not role.startswith("_"):
            # Re-find instrument for consistency.
            match = next((r for r in rows if r.instrument_token == sel.instrument_token), None)
            add(match, role)

    # Extra ATM PE + far OTM PE + mid strikes for NIFTY/RELIANCE.
    spot = selected.get("_meta_spot")
    spot_px = spot.strike if spot else 0.0
    for name in ("NIFTY", "RELIANCE"):
        add(_ce_atm(rows, name, expiry, spot_px), f"{name}_CE_ATM")
        pe_atm = None
        pes = [
            r
            for r in rows
            if r.name == name and r.instrument_type == "PE" and r.expiry == expiry and r.strike > 0
        ]
        if pes:
            if spot_px > 0:
                pe_atm = min(pes, key=lambda r: abs(r.strike - spot_px))
            else:
                pe_atm = sorted(pes, key=lambda r: r.strike)[len(pes) // 2]
        add(pe_atm, f"{name}_PE_ATM")
        far_ce = _ce_far_otm(rows, name, expiry)
        add(far_ce, f"{name}_CE_FAR")
        if pes:
            add(max(pes, key=lambda r: r.strike), f"{name}_PE_FAR")

    return watch[:12]


def latency_summary(calls: list[TimedCall]) -> dict[str, Any]:
    hist = [c.elapsed_s for c in calls if c.kind == "historical" and c.ok]
    hist_rows = [c.rows for c in calls if c.kind == "historical" and c.ok]
    quote = [c.elapsed_s for c in calls if c.kind in ("quote", "ltp") and c.ok]

    def pct(xs: list[float], p: float) -> float | None:
        if not xs:
            return None
        xs2 = sorted(xs)
        idx = min(len(xs2) - 1, max(0, int(round(p * (len(xs2) - 1)))))
        return round(xs2[idx], 3)

    return {
        "historical_ok": len(hist),
        "historical_fail": sum(1 for c in calls if c.kind == "historical" and not c.ok),
        "historical_median_s": round(statistics.median(hist), 3) if hist else None,
        "historical_p95_s": pct(hist, 0.95),
        "historical_mean_rows": round(statistics.mean(hist_rows), 1) if hist_rows else None,
        "quote_ok": len(quote),
        "quote_median_s": round(statistics.median(quote), 3) if quote else None,
    }


def _latest_probe_report() -> Path | None:
    if not PROBE_DIR.is_dir():
        return None
    files = sorted(PROBE_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    # Prefer non-expired reports that have watch_list.
    for f in files:
        if f.name.endswith("-expired.json"):
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("watch_list"):
            return f
    return files[0] if files else None


def run_expired_probe(paced: PacedKite) -> dict[str, Any]:
    latest = _latest_probe_report()
    if latest is None:
        return {"ok": False, "error": "no prior probe report with watch_list; run probe first"}
    data = json.loads(latest.read_text(encoding="utf-8"))
    watch = data.get("watch_list") or []
    if not watch:
        return {"ok": False, "error": f"no watch_list in {latest.name}"}

    end = _today_ist() - timedelta(days=1)
    start = end - timedelta(days=9)
    results: list[dict[str, Any]] = []
    for item in watch:
        token = int(item["instrument_token"])
        entry: dict[str, Any] = {
            "instrument_token": token,
            "tradingsymbol": item.get("tradingsymbol"),
            "note": item.get("note"),
            "source_report": latest.name,
        }
        for interval in ("day", "minute"):
            from_s = (
                start.isoformat()
                if interval == "day"
                else f"{start.isoformat()} 09:15:00"
            )
            to_s = end.isoformat() if interval == "day" else f"{end.isoformat()} 15:30:00"
            rows, call = paced.historical(token, from_s, to_s, interval, oi=True)
            status = "data" if call.ok and call.rows > 0 else ("empty" if call.ok else "error")
            entry[interval] = {
                "status": status,
                "rows": call.rows,
                "error": call.error,
            }
        results.append(entry)

    return {
        "ok": True,
        "source_report": latest.name,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "results": results,
        "summary": {
            "data": sum(1 for r in results if r.get("day", {}).get("status") == "data"),
            "empty": sum(1 for r in results if r.get("day", {}).get("status") == "empty"),
            "error": sum(1 for r in results if r.get("day", {}).get("status") == "error"),
        },
    }


def _print_summary(report: dict[str, Any]) -> None:
    print()
    print(f"{'check':<28} {'result':<12} value")
    print("-" * 72)
    checks = report.get("checks") or {}
    for name, body in checks.items():
        if name.startswith("_"):
            continue
        if name == "max_range_minute":
            print(f"{name:<28} {'ok':<12} max_span={body.get('max_ok_span_days')}")
        elif name == "max_range_day":
            print(f"{name:<28} {'ok':<12} max_span={body.get('max_ok_span_days')}")
        elif name == "oi":
            m = body.get("minute", {})
            d = body.get("day", {})
            print(
                f"{name:<28} {'ok' if m.get('has_oi_field') else 'fail':<12} "
                f"minute_oi={m.get('has_oi_field')} day_oi={d.get('has_oi_field')}"
            )
        elif name.startswith("sparsity_"):
            stats = body.get("stats") or {}
            days = stats.get("days") or []
            rows = ",".join(str(d.get("rows")) for d in days[:5])
            print(f"{name:<28} {'ok' if body.get('ok') else 'fail':<12} day_rows=[{rows}]")
        elif name == "listing":
            print(
                f"{name:<28} {'ok' if body.get('ok') else 'fail':<12} "
                f"first={body.get('first_candle')} pre_empty={body.get('pre_listing', {}).get('empty_not_error')}"
            )
        elif name == "day_vs_minute":
            print(
                f"{name:<28} {'ok' if body.get('volume_match') else 'diff':<12} "
                f"vol_day={body.get('day_volume')} vol_min={body.get('minute_volume_sum')}"
            )
        elif name == "quotes":
            print(
                f"{name:<28} {'ok' if body.get('ok') else 'fail':<12} "
                f"returned={body.get('returned')}/{body.get('requested')} "
                f"{body.get('elapsed_s')}s weekend={body.get('is_weekend')}"
            )
        elif name == "timestamps":
            print(
                f"{name:<28} {'ok' if body.get('ok') else 'fail':<12} "
                f"{body.get('first_time_ist')}-{body.get('last_time_ist')} tz={body.get('tzinfo')}"
            )
        elif name == "expired":
            s = body.get("summary") or {}
            print(
                f"{name:<28} {'ok' if body.get('ok') else 'fail':<12} "
                f"data={s.get('data')} empty={s.get('empty')} error={s.get('error')}"
            )
        else:
            print(f"{name:<28} {'':<12} (see report)")
    lat = report.get("latency") or {}
    print(
        f"{'latency':<28} {'':<12} "
        f"hist_median={lat.get('historical_median_s')}s "
        f"p95={lat.get('historical_p95_s')}s "
        f"mean_rows={lat.get('historical_mean_rows')}"
    )
    print()
    print(f"report: {report.get('report_path')}")


def run_probe(*, token: int | None = None, expired: bool = False) -> Path:
    try:
        kite = auth.get_kite()
    except (auth.NotLoggedIn, TokenException) as exc:
        raise SystemExit(str(exc)) from exc

    paced = PacedKite(kite)
    ensure_dir(PROBE_DIR)
    now = datetime.now(auth.IST)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    suffix = "-expired" if expired else ""
    report_path = PROBE_DIR / f"{stamp}{suffix}.json"

    report: dict[str, Any] = {
        "started_at": now.isoformat(),
        "mode": "expired" if expired else "full",
        "checks": {},
        "selected": {},
        "watch_list": [],
        "latency": {},
        "report_path": str(report_path),
    }

    try:
        if expired:
            report["checks"]["expired"] = run_expired_probe(paced)
            report["latency"] = latency_summary(paced.calls)
            report["finished_at"] = datetime.now(auth.IST).isoformat()
            report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
            _print_summary(report)
            return report_path

        selected = select_contracts(paced, token_override=token)
        report["selected"] = {
            k: asdict(v) for k, v in selected.items() if not k.startswith("_")
        }
        if "_meta_spot" in selected:
            report["spot_nifty"] = selected["_meta_spot"].strike

        index = selected.get("index")
        if index is not None:
            report["checks"]["max_range_minute"] = check_max_range(
                paced, index.instrument_token, "minute"
            )
            report["checks"]["max_range_day"] = check_max_range(
                paced, index.instrument_token, "day"
            )
        else:
            report["checks"]["max_range_minute"] = {"ok": False, "error": "NIFTY 50 index not found"}
            report["checks"]["max_range_day"] = {"ok": False, "error": "NIFTY 50 index not found"}

        idx_fut = selected.get("idx_fut")
        if idx_fut is not None:
            report["checks"]["oi"] = check_oi(paced, idx_fut)
            report["checks"]["day_vs_minute"] = check_day_vs_minute(paced, idx_fut)
            report["checks"]["timestamps"] = check_timestamps(paced, idx_fut)
        else:
            report["checks"]["oi"] = {"ok": False, "error": "idx_fut not found"}

        for role in ("idx_atm", "stk_far_otm"):
            sel = selected.get(role)
            if sel is not None:
                report["checks"][f"sparsity_{role}"] = check_sparsity(paced, sel)

        far = selected.get("stk_far_expiry")
        if far is not None:
            report["checks"]["listing"] = check_listing(paced, far)

        nfo = _nfo_rows()
        report["checks"]["quotes"] = check_quotes(paced, nfo)
        report["watch_list"] = build_watch_list(nfo, selected)
        report["latency"] = latency_summary(paced.calls)
        report["decision"] = _phase3_decision(report)

    except TokenException as exc:
        report["fatal"] = f"TokenException: {exc}"
        report["hint"] = "Run: uv run kitetools login"
        report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        raise SystemExit(f"Session invalid: {exc}. Run: uv run kitetools login") from exc
    except (NetworkException, DataException) as exc:
        report["fatal"] = f"{type(exc).__name__}: {exc}"
        report["latency"] = latency_summary(paced.calls)
        report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        raise SystemExit(f"Probe aborted: {exc}") from exc

    report["finished_at"] = datetime.now(auth.IST).isoformat()
    report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    _print_summary(report)
    return report_path


def _phase3_decision(report: dict[str, Any]) -> dict[str, Any]:
    minute_span = (report.get("checks") or {}).get("max_range_minute", {}).get("max_ok_span_days")
    day_span = (report.get("checks") or {}).get("max_range_day", {}).get("max_ok_span_days")
    use_day_prefetch = minute_span is not None and minute_span < 60
    return {
        "minute_max_span_days": minute_span,
        "day_max_span_days": day_span,
        "backfill_window_days": 60,
        "use_day_candle_prefetch": use_day_prefetch,
        "reason": (
            f"minute span {minute_span} < 60 → day-candle prefetch helps plan traded days"
            if use_day_prefetch
            else f"minute span {minute_span} >= 60 → can fetch minute directly for 60d backfill"
        ),
    }
