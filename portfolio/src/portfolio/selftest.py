"""Self-tests for portfolio backend core. No Kite login required.

Run: python -m portfolio.selftest
Uses data/samples/* and a temporary DuckDB file.
"""

from __future__ import annotations

import copy
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from kitetools.auth import IST
from kitetools.config import DATA_DIR

from . import fingerprint as fp
from . import store
from .fetch import RawPayload, load_from_sample
from .normalize import to_view
from .samples import SAMPLES_DIR, latest_sample_dir


class Failure(Exception):
    pass


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise Failure(msg)


def _sample_dirs() -> list[Path]:
    if not SAMPLES_DIR.is_dir():
        raise Failure(f"No samples under {SAMPLES_DIR}. Run: uv run portfolio sample")
    dirs = sorted((p for p in SAMPLES_DIR.iterdir() if p.is_dir()))
    _assert(len(dirs) >= 1, f"Need at least one sample under {SAMPLES_DIR}")
    return dirs


def _mutate_raw(raw: RawPayload) -> RawPayload:
    """Deep-copy raw so mutations do not touch files on disk."""
    return RawPayload(
        holdings=copy.deepcopy(raw.holdings),
        positions=copy.deepcopy(raw.positions),
        mf_holdings=copy.deepcopy(raw.mf_holdings),
        profile=dict(raw.profile),
        fetched_at=raw.fetched_at,
    )


def test_normalize_latest() -> None:
    folder = latest_sample_dir()
    raw = load_from_sample(folder)
    view = to_view(raw)
    _assert(len(view.holdings) == 28, f"expected 28 holdings, got {len(view.holdings)}")
    _assert(
        len(view.positions_net) == 14,
        f"expected 14 net positions, got {len(view.positions_net)}",
    )
    pledged = [h for h in view.holdings if h.quantity == 0 and h.collateral_quantity > 0]
    _assert(len(pledged) >= 1, "expected at least one fully pledged holding")
    for h in pledged:
        _assert(
            h.owned_quantity == h.collateral_quantity,
            f"{h.tradingsymbol} owned_quantity={h.owned_quantity} "
            f"!= collateral={h.collateral_quantity}",
        )
        _assert(h.owned_quantity > 0, f"{h.tradingsymbol} owned_quantity should be > 0")
    hdfc = [h for h in view.holdings if h.tradingsymbol == "HDFCBANK"]
    _assert(len(hdfc) == 1, "HDFCBANK missing from sample")
    _assert(hdfc[0].owned_quantity == 470, f"HDFCBANK owned={hdfc[0].owned_quantity}")
    print("  ok normalize_latest")


def test_fingerprint_stable_across_samples() -> None:
    dirs = _sample_dirs()
    _assert(len(dirs) >= 2, "need two samples to compare fingerprints")
    hashes = []
    for d in dirs:
        view = to_view(load_from_sample(d))
        hashes.append(fp.fingerprint_hash(fp.composition(view)))
    _assert(
        len(set(hashes)) == 1,
        f"sample fingerprints differ: {hashes}",
    )
    print(f"  ok fingerprint_stable hash={hashes[0][:8]} (n={len(dirs)})")


def test_price_change_ignored_qty_change_triggers() -> None:
    raw = _mutate_raw(load_from_sample(latest_sample_dir()))
    view = to_view(raw)
    base = fp.composition(view)
    base_hash = fp.fingerprint_hash(base)

    # Price-only mutation
    raw.holdings[0]["last_price"] = float(raw.holdings[0].get("last_price") or 0) + 999
    raw.holdings[0]["pnl"] = float(raw.holdings[0].get("pnl") or 0) + 999
    if raw.positions.get("net"):
        raw.positions["net"][0]["last_price"] = (
            float(raw.positions["net"][0].get("last_price") or 0) + 999
        )
        raw.positions["net"][0]["pnl"] = float(raw.positions["net"][0].get("pnl") or 0) + 999
    price_view = to_view(raw)
    price_comp = fp.composition(price_view)
    _assert(
        fp.fingerprint_hash(price_comp) == base_hash,
        "price-only change must not alter fingerprint",
    )

    # Quantity mutation
    raw2 = _mutate_raw(load_from_sample(latest_sample_dir()))
    target = raw2.holdings[0]
    old_qty = int(target.get("quantity") or 0)
    target["quantity"] = old_qty + 5
    qty_view = to_view(raw2)
    qty_comp = fp.composition(qty_view)
    qty_hash = fp.fingerprint_hash(qty_comp)
    _assert(qty_hash != base_hash, "quantity change must alter fingerprint")
    delta = fp.diff(base, qty_comp)
    isin = target.get("isin")
    key = f"EQ:{isin}"
    _assert(key in delta.changed, f"diff.changed missing {key}: {delta.changed}")
    old_v, new_v = delta.changed[key]
    _assert(new_v - old_v == 5, f"expected +5 on {key}, got {old_v}->{new_v}")
    print("  ok price_ignored_qty_triggers")


def test_zero_positions_excluded_and_cnc_merge() -> None:
    raw = _mutate_raw(load_from_sample(latest_sample_dir()))
    # Inject a zero-qty F&O row — must not appear in composition
    raw.positions.setdefault("net", []).append(
        {
            "tradingsymbol": "ZEROTEST26DECFUT",
            "exchange": "NFO",
            "instrument_token": 1,
            "product": "NRML",
            "segment": "FO",
            "quantity": 0,
            "overnight_quantity": 0,
            "multiplier": 1,
            "average_price": 0,
            "last_price": 0,
            "close_price": 0,
            "value": 0,
            "pnl": 0,
            "m2m": 0,
            "realised": 0,
            "unrealised": 0,
        }
    )
    view = to_view(raw)
    comp = fp.composition(view)
    _assert(
        "POS:NFO:ZEROTEST26DECFUT:NRML" not in comp,
        "zero-qty position must be dropped",
    )

    # CNC net position merges into holding ISIN
    holding = raw.holdings[0]
    isin = holding["isin"]
    exchange = holding["exchange"]
    symbol = holding["tradingsymbol"]
    before = fp.composition(to_view(_mutate_raw(load_from_sample(latest_sample_dir()))))
    before_qty = before.get(f"EQ:{isin}", 0)

    raw3 = _mutate_raw(load_from_sample(latest_sample_dir()))
    raw3.positions.setdefault("net", []).append(
        {
            "tradingsymbol": symbol,
            "exchange": exchange,
            "instrument_token": holding.get("instrument_token") or 0,
            "product": "CNC",
            "segment": "EQ",
            "quantity": 7,
            "overnight_quantity": 0,
            "multiplier": 1,
            "average_price": 100,
            "last_price": 100,
            "close_price": 100,
            "value": 700,
            "pnl": 0,
            "m2m": 0,
            "realised": 0,
            "unrealised": 0,
        }
    )
    after = fp.composition(to_view(raw3))
    after_qty = after.get(f"EQ:{isin}", 0)
    _assert(
        after_qty == before_qty + 7,
        f"CNC merge failed: before={before_qty} after={after_qty}",
    )
    _assert(
        f"EQ:{exchange}:{symbol}:CNC" not in after,
        "CNC with known ISIN must not use fallback key",
    )
    print("  ok zero_excluded_cnc_merge")


def test_mf_rounding_and_change() -> None:
    raw = _mutate_raw(load_from_sample(latest_sample_dir()))
    raw.mf_holdings = [
        {
            "isin": "INF123456789",
            "folio": "12345678",
            "fund": "Test Fund",
            "tradingsymbol": "INF123456789",
            "quantity": 10.12345,
            "average_price": 100,
            "last_price": 110,
            "pnl": 100,
        }
    ]
    view = to_view(raw)
    comp = fp.composition(view)
    key = "MF:INF123456789:12345678"
    _assert(key in comp, f"missing MF key {key}")
    _assert(comp[key] == 10.123, f"expected round to 3dp, got {comp[key]}")

    base_hash = fp.fingerprint_hash(comp)
    raw.mf_holdings[0]["quantity"] = 10.124
    # 10.12345 -> 10.123; 10.124 -> 10.124 — different
    view2 = to_view(raw)
    comp2 = fp.composition(view2)
    _assert(comp2[key] == 10.124, f"got {comp2[key]}")
    _assert(
        fp.fingerprint_hash(comp2) != base_hash,
        "MF unit change must alter fingerprint",
    )

    # Tiny change that rounds to same 3dp must NOT trigger
    raw.mf_holdings[0]["quantity"] = 10.12349
    view3 = to_view(raw)
    _assert(
        fp.fingerprint_hash(fp.composition(view3)) == base_hash,
        "sub-3dp MF noise must not alter fingerprint",
    )
    print("  ok mf_rounding_and_change")


def test_capture_idempotence() -> None:
    folder = latest_sample_dir()
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.duckdb"
        con = store.connect(db)
        try:
            store.ensure_schema(con)
            view = to_view(load_from_sample(folder))
            r1 = store.capture(con, view, source="sample")
            _assert(r1.saved, "first capture must save")
            _assert(r1.snapshot_id == 1, f"expected id=1, got {r1.snapshot_id}")

            r2 = store.capture(con, view, source="sample")
            _assert(not r2.saved, "second identical capture must not save")
            _assert(r2.snapshot_id == 1, "should point at existing snapshot")

            # Mutate quantity and capture again
            raw = _mutate_raw(load_from_sample(folder))
            raw.holdings[0]["quantity"] = int(raw.holdings[0].get("quantity") or 0) + 1
            # Bump fetched_at so observed_at differs
            raw.fetched_at = datetime.now(IST)
            view2 = to_view(raw)
            r3 = store.capture(con, view2, source="sample")
            _assert(r3.saved, "qty change must save new snapshot")
            _assert(r3.snapshot_id == 2, f"expected id=2, got {r3.snapshot_id}")

            rows = store.list_snapshots(con)
            _assert(len(rows) == 2, f"expected 2 snapshots, got {len(rows)}")

            rec = store.get_snapshot(con, 1)
            _assert(rec.meta.counts["holdings"] == 28, f"round-trip counts={rec.meta.counts}")
            _assert(rec.meta.counts["positions_net"] == 14, f"round-trip net={rec.meta.counts}")
            _assert(len(rec.view.holdings) == 28, "get_snapshot holdings len")
            _assert(len(rec.composition) > 0, "composition empty")
        finally:
            con.close()
    print("  ok capture_idempotence")


def main() -> int:
    print(f"portfolio.selftest  samples={SAMPLES_DIR}  data={DATA_DIR}")
    tests = [
        test_normalize_latest,
        test_fingerprint_stable_across_samples,
        test_price_change_ignored_qty_change_triggers,
        test_zero_positions_excluded_and_cnc_merge,
        test_mf_rounding_and_change,
        test_capture_idempotence,
    ]
    failed = 0
    for test in tests:
        name = test.__name__
        try:
            test()
        except Failure as exc:
            print(f"  FAIL {name}: {exc}")
            failed += 1
        except Exception as exc:
            print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
            failed += 1
    if failed:
        print(f"{failed}/{len(tests)} failed")
        return 1
    print(f"All {len(tests)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
