"""Phase-0 probe for MCX and CDS historical / quote behaviour.

Fetches exchange instrument dumps directly (bypasses instruments.scope).
Writes JSON under data/ohlcv/{mcx,cds}/_probe/. Does not store candles.
"""

from __future__ import annotations

import json
import statistics
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from kitetools import auth
from kitetools.config import DATA_DIR

from .paths import ensure_dir, paths_for
from .probe import (
    RANGE_SPANS_DAYS,
    PacedKite,
    TimedCall,
    _candle_date,
    _today_ist,
)


@dataclass(frozen=True)
class Sel:
    role: str
    exchange: str
    instrument_token: int
    tradingsymbol: str
    name: str
    expiry: str | None
    instrument_type: str
    strike: float
    segment: str
    lot_size: int
    tick_size: float


def _probe_dir(exchange: str) -> Path:
    return paths_for(exchange.lower()).probe_dir


def _parse_expiry(raw: Any) -> date | None:
    if raw is None or raw == "" or raw == "0001-01-01":
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    try:
        return datetime.fromisoformat(str(raw)[:10]).date()
    except ValueError:
        return None


def _row_to_sel(role: str, r: dict[str, Any]) -> Sel:
    exp = _parse_expiry(r.get("expiry"))
    return Sel(
        role=role,
        exchange=str(r.get("exchange") or ""),
        instrument_token=int(r["instrument_token"]),
        tradingsymbol=str(r.get("tradingsymbol") or ""),
        name=str(r.get("name") or ""),
        expiry=exp.isoformat() if exp else None,
        instrument_type=str(r.get("instrument_type") or ""),
        strike=float(r.get("strike") or 0),
        segment=str(r.get("segment") or ""),
        lot_size=int(r.get("lot_size") or 0),
        tick_size=float(r.get("tick_size") or 0),
    )


def fetch_exchange_rows(kite: Any, exchange: str) -> list[dict[str, Any]]:
    return list(kite.instruments(exchange) or [])


def inventory(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_seg = Counter(str(r.get("segment") or "") for r in rows)
    by_type = Counter(str(r.get("instrument_type") or "") for r in rows)
    names = Counter(str(r.get("name") or "") for r in rows if r.get("name"))
    expiries = sorted(
        {d.isoformat() for r in rows if (d := _parse_expiry(r.get("expiry"))) is not None}
    )
    return {
        "count": len(rows),
        "segments": dict(by_seg.most_common()),
        "instrument_types": dict(by_type.most_common()),
        "top_names": names.most_common(25),
        "unique_names": len(names),
        "expiry_count": len(expiries),
        "nearest_expiries": expiries[:8],
        "farthest_expiries": expiries[-5:] if expiries else [],
        "sample_keys": sorted(rows[0].keys()) if rows else [],
    }


def select_contracts(rows: list[dict[str, Any]], exchange: str) -> dict[str, Sel]:
    """Pick liquid FUT + one option for session/OI/range checks."""
    out: dict[str, Sel] = {}
    preferred = {
        "MCX": ["GOLD", "CRUDEOIL", "SILVER", "NATURALGAS", "COPPER"],
        "CDS": ["USDINR", "EURINR", "GBPINR", "JPYINR"],
    }.get(exchange, [])

    futs = [
        r
        for r in rows
        if str(r.get("instrument_type") or "") == "FUT" and _parse_expiry(r.get("expiry"))
    ]
    opts = [
        r
        for r in rows
        if str(r.get("instrument_type") or "") in ("CE", "PE")
        and _parse_expiry(r.get("expiry"))
    ]

    def nearest_fut(name: str) -> dict[str, Any] | None:
        pool = [r for r in futs if str(r.get("name") or "") == name]
        if not pool:
            return None
        return sorted(pool, key=lambda r: _parse_expiry(r.get("expiry")) or date.max)[0]

    for i, name in enumerate(preferred):
        fut = nearest_fut(name)
        if fut is None:
            continue
        role = "fut_primary" if i == 0 else f"fut_{name.lower()}"
        if role in out:
            role = f"fut_{name.lower()}_{i}"
        out[role if i > 0 else "fut"] = _row_to_sel("fut" if i == 0 else role, fut)
        if i == 0:
            # ATM-ish CE on same expiry for the primary underlying.
            exp = _parse_expiry(fut.get("expiry"))
            spot = float(fut.get("last_price") or 0)
            ces = [
                r
                for r in opts
                if str(r.get("name") or "") == name
                and _parse_expiry(r.get("expiry")) == exp
                and float(r.get("strike") or 0) > 0
            ]
            if ces:
                if spot > 0:
                    ce = min(ces, key=lambda r: abs(float(r.get("strike") or 0) - spot))
                else:
                    ces_sorted = sorted(ces, key=lambda r: float(r.get("strike") or 0))
                    ce = ces_sorted[len(ces_sorted) // 2]
                out["opt_atm"] = _row_to_sel("opt_atm", ce)
            # Farthest FUT for listing / continuous contrast.
            pool = [r for r in futs if str(r.get("name") or "") == name]
            if len(pool) > 1:
                far = sorted(pool, key=lambda r: _parse_expiry(r.get("expiry")) or date.min)[-1]
                out["fut_far"] = _row_to_sel("fut_far", far)

    # Indices if any (MCX sometimes lists them).
    idx = [
        r
        for r in rows
        if str(r.get("segment") or "").upper() in ("INDICES", f"{exchange}-INDEX")
        or str(r.get("instrument_type") or "").upper() in ("EQ", "INDEX")
        and "INDEX" in str(r.get("segment") or "").upper()
    ]
    # Broader: segment containing INDEX
    idx = [r for r in rows if "INDEX" in str(r.get("segment") or "").upper()]
    if idx:
        out["index"] = _row_to_sel("index", idx[0])

    if "fut" not in out and futs:
        # Fallback: most common name's nearest FUT.
        name_counts = Counter(str(r.get("name") or "") for r in futs)
        top = name_counts.most_common(1)[0][0]
        fut = nearest_fut(top)
        if fut is not None:
            out["fut"] = _row_to_sel("fut", fut)

    return out


def check_max_range(
    paced: PacedKite,
    token: int,
    interval: str,
    *,
    from_hhmm: str = "00:00:00",
    to_hhmm: str = "23:59:00",
) -> dict[str, Any]:
    end = _today_ist() - timedelta(days=1)
    results: list[dict[str, Any]] = []
    max_ok: int | None = None
    first_fail: dict[str, Any] | None = None
    for span in RANGE_SPANS_DAYS:
        start = end - timedelta(days=span - 1)
        from_s = f"{start.isoformat()} {from_hhmm}"
        to_s = f"{end.isoformat()} {to_hhmm}"
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
            if call.error and "InputException" in call.error:
                first_fail = entry
                break
            if first_fail is None:
                first_fail = entry
    return {
        "interval": interval,
        "token": token,
        "max_ok_span_days": max_ok,
        "first_fail": first_fail,
        "trials": results,
    }


def check_oi(paced: PacedKite, sel: Sel) -> dict[str, Any]:
    end = _today_ist() - timedelta(days=1)
    start = end - timedelta(days=4)
    out: dict[str, Any] = {
        "token": sel.instrument_token,
        "symbol": sel.tradingsymbol,
        "exchange": sel.exchange,
    }
    for interval in ("minute", "day"):
        from_s = f"{start.isoformat()} 00:00:00"
        to_s = f"{end.isoformat()} 23:59:00"
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


def check_session_hours(paced: PacedKite, sel: Sel) -> dict[str, Any]:
    """Pull a recent trading day with a full-calendar window to see actual hours."""
    day = _today_ist() - timedelta(days=1)
    days_out: list[dict[str, Any]] = []
    for _ in range(14):
        from_s = f"{day.isoformat()} 00:00:00"
        to_s = f"{day.isoformat()} 23:59:00"
        rows, call = paced.historical(sel.instrument_token, from_s, to_s, "minute", oi=True)
        if call.ok and rows:
            rows_sorted = sorted(rows, key=lambda r: r["date"])
            first = rows_sorted[0]["date"]
            last = rows_sorted[-1]["date"]
            vols = [int(r.get("volume") or 0) for r in rows_sorted]
            hours = Counter()
            for r in rows_sorted:
                ts = r["date"]
                if isinstance(ts, datetime):
                    hours[ts.hour] += 1
            days_out.append(
                {
                    "day": day.isoformat(),
                    "weekday": day.strftime("%A"),
                    "rows": len(rows_sorted),
                    "first": str(first),
                    "last": str(last),
                    "first_hhmm": first.strftime("%H:%M") if isinstance(first, datetime) else None,
                    "last_hhmm": last.strftime("%H:%M") if isinstance(last, datetime) else None,
                    "volume_sum": sum(vols),
                    "zero_vol_rows": sum(1 for v in vols if v == 0),
                    "hour_histogram": {str(h): hours[h] for h in sorted(hours)},
                }
            )
            if len(days_out) >= 3:
                break
        day -= timedelta(days=1)

    return {
        "token": sel.instrument_token,
        "symbol": sel.tradingsymbol,
        "exchange": sel.exchange,
        "days": days_out,
        "ok": bool(days_out),
    }


def check_continuous(paced: PacedKite, sel: Sel) -> dict[str, Any]:
    end = _today_ist() - timedelta(days=1)
    start_day = end - timedelta(days=2000)
    start_min = end - timedelta(days=30)
    out: dict[str, Any] = {
        "token": sel.instrument_token,
        "symbol": sel.tradingsymbol,
        "exchange": sel.exchange,
    }
    # Day continuous long span.
    rows, call = paced.historical(
        sel.instrument_token,
        start_day.isoformat(),
        end.isoformat(),
        "day",
        oi=True,
        continuous=True,
    )
    first = _candle_date(rows[0]) if rows else None
    last = _candle_date(rows[-1]) if rows else None
    out["day_continuous"] = {
        "ok": call.ok,
        "error": call.error,
        "rows": call.rows,
        "from": start_day.isoformat(),
        "to": end.isoformat(),
        "first": first.isoformat() if first else None,
        "last": last.isoformat() if last else None,
        "span_calendar_days": (last - first).days if first and last else None,
    }
    # Day without continuous (same span) for contrast.
    rows2, call2 = paced.historical(
        sel.instrument_token,
        start_day.isoformat(),
        end.isoformat(),
        "day",
        oi=True,
        continuous=False,
    )
    first2 = _candle_date(rows2[0]) if rows2 else None
    last2 = _candle_date(rows2[-1]) if rows2 else None
    out["day_contract"] = {
        "ok": call2.ok,
        "error": call2.error,
        "rows": call2.rows,
        "first": first2.isoformat() if first2 else None,
        "last": last2.isoformat() if last2 else None,
    }
    # Minute + continuous should fail or ignore for expired stitching.
    rows3, call3 = paced.historical(
        sel.instrument_token,
        f"{start_min.isoformat()} 00:00:00",
        f"{end.isoformat()} 23:59:00",
        "minute",
        oi=False,
        continuous=True,
    )
    out["minute_continuous"] = {
        "ok": call3.ok,
        "error": call3.error,
        "rows": call3.rows,
        "note": "Docs: continuous is day-only for NFO/MCX FUT",
    }
    return out


def check_listing(paced: PacedKite, sel: Sel) -> dict[str, Any]:
    end = _today_ist() - timedelta(days=1)
    start = end - timedelta(days=400)
    rows, call = paced.historical(
        sel.instrument_token, start.isoformat(), end.isoformat(), "day", oi=True
    )
    first = _candle_date(rows[0]) if rows else None
    last = _candle_date(rows[-1]) if rows else None
    pre: dict[str, Any]
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
        pre = {
            "from": pre_start.isoformat(),
            "to": pre_end.isoformat(),
            "ok": pre_call.ok,
            "rows": pre_call.rows,
            "error": pre_call.error,
            "empty_not_error": pre_call.ok and pre_call.rows == 0,
        }
    else:
        pre = {"skipped": True, "reason": "no candles in window"}
    return {
        "token": sel.instrument_token,
        "symbol": sel.tradingsymbol,
        "ok": call.ok,
        "error": call.error,
        "rows": call.rows,
        "first_candle": first.isoformat() if first else None,
        "last_candle": last.isoformat() if last else None,
        "pre_listing": pre,
    }


def check_day_vs_minute(paced: PacedKite, sel: Sel) -> dict[str, Any]:
    day = _today_ist() - timedelta(days=1)
    day_rows: list[dict[str, Any]] = []
    day_call: TimedCall | None = None
    for _ in range(14):
        day_rows, day_call = paced.historical(
            sel.instrument_token, day.isoformat(), day.isoformat(), "day", oi=True
        )
        if day_call.ok and day_rows:
            break
        day -= timedelta(days=1)
    else:
        return {
            "token": sel.instrument_token,
            "symbol": sel.tradingsymbol,
            "ok": False,
            "error": "no day candle in last 14 calendar days",
        }

    from_s = f"{day.isoformat()} 00:00:00"
    to_s = f"{day.isoformat()} 23:59:00"
    min_rows, min_call = paced.historical(
        sel.instrument_token, from_s, to_s, "minute", oi=True
    )
    day_vol = int(day_rows[0].get("volume") or 0) if day_rows else None
    min_vol = sum(int(r.get("volume") or 0) for r in min_rows)
    day_oi = day_rows[0].get("oi") if day_rows else None
    last_min_oi = min_rows[-1].get("oi") if min_rows else None
    first_ts = min_rows[0]["date"] if min_rows else None
    last_ts = min_rows[-1]["date"] if min_rows else None
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
        "minute_first": str(first_ts) if first_ts else None,
        "minute_last": str(last_ts) if last_ts else None,
        "day_error": day_call.error if day_call else None,
        "minute_error": min_call.error,
    }


def check_quotes(paced: PacedKite, rows: list[dict[str, Any]], exchange: str) -> dict[str, Any]:
    with_exp = [r for r in rows if _parse_expiry(r.get("expiry")) is not None]
    if not with_exp:
        return {"ok": False, "error": f"no {exchange} rows with expiry"}
    nearest = min(_parse_expiry(r.get("expiry")) for r in with_exp)  # type: ignore[type-var]
    pool = [r for r in with_exp if _parse_expiry(r.get("expiry")) == nearest]
    pool_sorted = sorted(
        pool,
        key=lambda r: (
            0 if str(r.get("instrument_type")) == "FUT" else 1,
            str(r.get("name") or ""),
            float(r.get("strike") or 0),
        ),
    )
    batch = pool_sorted[: min(200, len(pool_sorted))]
    keys = [f"{exchange}:{r['tradingsymbol']}" for r in batch]
    data, call = paced.quote(keys)
    samples: list[dict[str, Any]] = []
    for key, q in list(data.items())[:5]:
        ohlc = q.get("ohlc") or {}
        samples.append(
            {
                "key": key,
                "last_price": q.get("last_price"),
                "ohlc_close": ohlc.get("close"),
                "volume": q.get("volume"),
                "average_price": q.get("average_price"),
                "oi": q.get("oi"),
                "timestamp": str(q.get("timestamp")),
                "last_trade_time": str(q.get("last_trade_time")),
            }
        )
    today = _today_ist()
    return {
        "ok": call.ok,
        "error": call.error,
        "requested": len(keys),
        "returned": call.rows,
        "elapsed_s": round(call.elapsed_s, 3),
        "nearest_expiry": nearest.isoformat() if nearest else None,
        "run_date_ist": today.isoformat(),
        "weekday": today.strftime("%A"),
        "samples": samples,
    }


def probe_exchange(paced: PacedKite, kite: Any, exchange: str) -> dict[str, Any]:
    t0 = time.monotonic()
    print(f"\n=== {exchange}: fetching instruments ===")
    rows = fetch_exchange_rows(kite, exchange)
    inv = inventory(rows)
    print(f"  {inv['count']} instruments, {inv['unique_names']} names, types={inv['instrument_types']}")

    selected = select_contracts(rows, exchange)
    print(f"  selected: {list(selected.keys())}")
    for role, sel in selected.items():
        print(
            f"    {role}: {sel.tradingsymbol} token={sel.instrument_token} "
            f"exp={sel.expiry} type={sel.instrument_type}"
        )

    report: dict[str, Any] = {
        "exchange": exchange,
        "probed_at": datetime.now(auth.IST).isoformat(),
        "inventory": inv,
        "selected": {k: asdict(v) for k, v in selected.items()},
        "checks": {},
    }

    fut = selected.get("fut")
    if fut is None:
        report["checks"]["error"] = "no FUT selected"
        report["elapsed_s"] = round(time.monotonic() - t0, 2)
        return report

    print(f"  max_range minute/day on {fut.tradingsymbol} ...")
    report["checks"]["max_range_minute"] = check_max_range(
        paced, fut.instrument_token, "minute"
    )
    report["checks"]["max_range_day"] = check_max_range(paced, fut.instrument_token, "day")
    print(
        f"    minute max_ok={report['checks']['max_range_minute'].get('max_ok_span_days')} "
        f"day max_ok={report['checks']['max_range_day'].get('max_ok_span_days')}"
    )

    print("  oi ...")
    report["checks"]["oi_fut"] = check_oi(paced, fut)
    if "opt_atm" in selected:
        report["checks"]["oi_opt"] = check_oi(paced, selected["opt_atm"])

    print("  session_hours ...")
    report["checks"]["session_hours_fut"] = check_session_hours(paced, fut)
    if "opt_atm" in selected:
        report["checks"]["session_hours_opt"] = check_session_hours(paced, selected["opt_atm"])
    if "index" in selected:
        report["checks"]["session_hours_index"] = check_session_hours(
            paced, selected["index"]
        )

    print("  continuous ...")
    report["checks"]["continuous_fut"] = check_continuous(paced, fut)

    print("  listing ...")
    listing_target = selected.get("fut_far") or fut
    report["checks"]["listing"] = check_listing(paced, listing_target)

    print("  day_vs_minute ...")
    report["checks"]["day_vs_minute"] = check_day_vs_minute(paced, fut)

    print("  quote sample ...")
    report["checks"]["quote"] = check_quotes(paced, rows, exchange)

    # Latency summary from paced calls during this exchange section.
    hist = [c for c in paced.calls if c.kind == "historical" and c.ok]
    # Approximate: use last N calls — better store offset. Use all ok historical for now
    # filtered by counting before/after would be cleaner; good enough for Phase 0.
    if hist:
        el = [c.elapsed_s for c in hist[-40:]]
        report["latency_historical_recent"] = {
            "n": len(el),
            "median_s": round(statistics.median(el), 3),
            "p95_s": round(sorted(el)[max(0, int(len(el) * 0.95) - 1)], 3),
            "max_s": round(max(el), 3),
        }

    report["elapsed_s"] = round(time.monotonic() - t0, 2)
    print(f"  done in {report['elapsed_s']}s")
    return report


def run(*, exchanges: tuple[str, ...] = ("MCX", "CDS")) -> Path:
    kite = auth.get_kite()
    paced = PacedKite(kite)
    stamp = datetime.now(auth.IST).strftime("%Y%m%d-%H%M%S")
    combined: dict[str, Any] = {
        "probed_at": datetime.now(auth.IST).isoformat(),
        "exchanges": {},
        "call_summary": {},
    }

    for exch in exchanges:
        report = probe_exchange(paced, kite, exch)
        combined["exchanges"][exch] = report
        out_dir = ensure_dir(_probe_dir(exch))
        path = out_dir / f"{stamp}.json"
        path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(f"Wrote {path}")

    ok = [c for c in paced.calls if c.ok]
    fail = [c for c in paced.calls if not c.ok]
    combined["call_summary"] = {
        "total": len(paced.calls),
        "ok": len(ok),
        "fail": len(fail),
        "fail_samples": [
            {"kind": c.kind, "error": c.error, "elapsed_s": round(c.elapsed_s, 3)}
            for c in fail[:10]
        ],
    }
    combo_dir = ensure_dir(DATA_DIR / "ohlcv" / "_probe")
    combo_path = combo_dir / f"mcx_cds_{stamp}.json"
    combo_path.write_text(json.dumps(combined, indent=2, default=str), encoding="utf-8")
    print(f"\nCombined report: {combo_path}")
    return combo_path


def main() -> None:
    run()


if __name__ == "__main__":
    main()
