"""Self-tests for portfolio backend core. No Kite login required.

Run: python -m portfolio.selftest
Uses data/samples/* and a temporary DuckDB file.
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

import instruments
from instruments import InstrumentMeta
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
    # Force empty lookup so offline runs never hit Kite / on-disk instruments cache.
    instruments.set_lookup_override({})
    try:
        view = to_view(raw)
    finally:
        instruments.clear_lookup_override()
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
    for p in view.positions_net:
        _assert(p.expiry is None, f"empty override should leave expiry null: {p.tradingsymbol}")
        _assert(
            p.instrument_type is None,
            f"empty override should leave instrument_type null: {p.tradingsymbol}",
        )
    print("  ok normalize_latest")


def _instrument_type_from_symbol(symbol: str) -> str:
    if symbol.endswith("CE"):
        return "CE"
    if symbol.endswith("PE"):
        return "PE"
    if symbol.endswith("FUT"):
        return "FUT"
    return "EQ"


def test_position_enrichment() -> None:
    raw = load_from_sample(latest_sample_dir())
    stub: dict[int, InstrumentMeta] = {}
    for row in raw.positions.get("net") or []:
        token = int(row["instrument_token"])
        symbol = str(row["tradingsymbol"])
        if str(row.get("segment") or "") == "FO" or str(row.get("exchange") or "") == "NFO":
            stub[token] = InstrumentMeta(
                expiry=date(2026, 11, 3),
                instrument_type=_instrument_type_from_symbol(symbol),
            )
        else:
            stub[token] = InstrumentMeta(expiry=None, instrument_type="EQ")

    instruments.set_lookup_override(stub)
    try:
        view = to_view(raw)
        fo = [
            p
            for p in view.positions_net
            if p.segment == "FO" or p.exchange == "NFO"
        ]
        _assert(len(fo) >= 1, "expected F&O rows in sample")
        for p in fo:
            _assert(p.expiry == date(2026, 11, 3), f"{p.tradingsymbol} expiry={p.expiry}")
            _assert(
                p.instrument_type in ("CE", "PE", "FUT"),
                f"{p.tradingsymbol} instrument_type={p.instrument_type}",
            )
        from . import serialize

        payload = serialize.portfolio_payload(view, source="live")
        row0 = payload["positions"]["net"][0]
        _assert(row0.get("expiry") == "2026-11-03", f"serialized expiry={row0.get('expiry')}")
        _assert(
            isinstance(row0.get("instrument_type"), str),
            f"instrument_type={row0.get('instrument_type')}",
        )
    finally:
        instruments.clear_lookup_override()
    print("  ok position_enrichment")


def test_schema_migrate_v1_to_v2() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "v1.duckdb"
        con = store.connect(db_path)
        try:
            con.execute(
                """
                CREATE TABLE meta (
                    key VARCHAR PRIMARY KEY,
                    value VARCHAR NOT NULL
                )
                """
            )
            con.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', '1')"
            )
            con.execute(
                """
                CREATE TABLE snapshot_positions (
                    snapshot_id BIGINT NOT NULL,
                    kind VARCHAR NOT NULL,
                    exchange VARCHAR,
                    tradingsymbol VARCHAR,
                    instrument_token BIGINT,
                    product VARCHAR,
                    segment VARCHAR,
                    quantity BIGINT,
                    overnight_quantity BIGINT,
                    multiplier BIGINT,
                    average_price DOUBLE,
                    last_price DOUBLE,
                    close_price DOUBLE,
                    value DOUBLE,
                    pnl DOUBLE,
                    m2m DOUBLE,
                    realised DOUBLE,
                    unrealised DOUBLE,
                    buy_quantity BIGINT,
                    buy_price DOUBLE,
                    buy_value DOUBLE,
                    sell_quantity BIGINT,
                    sell_price DOUBLE,
                    sell_value DOUBLE,
                    day_buy_quantity BIGINT,
                    day_buy_price DOUBLE,
                    day_buy_value DOUBLE,
                    day_sell_quantity BIGINT,
                    day_sell_price DOUBLE,
                    day_sell_value DOUBLE
                )
                """
            )
            store.ensure_schema(con)
            ver = con.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            _assert(ver is not None and int(ver[0]) == 2, f"schema_version={ver}")
            cols = {
                str(r[1])
                for r in con.execute("PRAGMA table_info('snapshot_positions')").fetchall()
            }
            _assert("expiry" in cols, f"missing expiry in {cols}")
            _assert("instrument_type" in cols, f"missing instrument_type in {cols}")
        finally:
            con.close()
    print("  ok schema_migrate_v1_to_v2")


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


class _CountingFetcher:
    """Sample-backed fetcher that counts calls for cache tests."""

    def __init__(self, folder: Path | None = None) -> None:
        self.folder = folder or latest_sample_dir()
        self.calls = 0
        self._override: RawPayload | None = None

    def set_override(self, raw: RawPayload | None) -> None:
        self._override = raw

    def __call__(self) -> RawPayload:
        self.calls += 1
        if self._override is not None:
            return self._override
        return load_from_sample(self.folder)


def _api_client(
    tmp: Path,
    fetcher: _CountingFetcher,
    ttl: float = 10.0,
    *,
    ui_dist: Path | None = None,
):
    from fastapi.testclient import TestClient

    from .api import create_app

    db = tmp / "api.duckdb"
    # Default: no SPA so health/UI tests stay deterministic regardless of a local build.
    dist = ui_dist if ui_dist is not None else tmp / "no-ui-dist"
    app = create_app(db_path=db, fetcher=fetcher, ttl=ttl, ui_dist=dist)
    return TestClient(app), db


def test_api_health() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client, _db = _api_client(Path(tmp), _CountingFetcher())
        r = client.get("/api/health")
        _assert(r.status_code == 200, f"health status={r.status_code}")
        body = r.json()
        _assert(body.get("ok") is True, f"health ok={body.get('ok')}")
        blob = json.dumps(body)
        _assert("access_token" not in blob, "health must not leak access_token")
        _assert("session" in body and "db" in body, "health missing session/db")
        _assert(body.get("ui_dist_exists") is False, f"ui_dist_exists={body.get('ui_dist_exists')}")
        root = client.get("/")
        _assert(root.status_code == 200, f"root status={root.status_code}")
        root_body = root.json()
        _assert("hint" in root_body, f"root hint missing: {root_body}")
    print("  ok api_health")


def test_api_spa_mount() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        dist = base / "ui" / "dist"
        assets = dist / "assets"
        assets.mkdir(parents=True)
        (dist / "index.html").write_text(
            "<!doctype html><html><body>spa</body></html>",
            encoding="utf-8",
        )
        (assets / "app.js").write_text("console.log(1)", encoding="utf-8")

        client, _db = _api_client(base, _CountingFetcher(), ui_dist=dist)
        health = client.get("/api/health")
        _assert(health.status_code == 200, f"health={health.status_code}")
        _assert(health.json().get("ui_dist_exists") is True, "ui_dist_exists should be true")

        # API still works when SPA is mounted.
        live = client.get("/api/portfolio/live")
        _assert(live.status_code == 200, f"live under spa={live.status_code}")

        index = client.get("/portfolio")
        _assert(index.status_code == 200, f"spa /portfolio={index.status_code}")
        _assert("spa" in index.text, f"spa body unexpected: {index.text[:80]}")

        deep = client.get("/snapshots/1")
        _assert(deep.status_code == 200, f"spa deep link={deep.status_code}")
        _assert("spa" in deep.text, "deep link must return index.html")

        asset = client.get("/assets/app.js")
        _assert(asset.status_code == 200, f"asset={asset.status_code}")
        _assert("console.log" in asset.text, f"asset body={asset.text}")
    print("  ok api_spa_mount")


def test_api_live_and_cache() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fetcher = _CountingFetcher()
        client, _db = _api_client(Path(tmp), fetcher, ttl=30.0)

        r1 = client.get("/api/portfolio/live")
        _assert(r1.status_code == 200, f"live status={r1.status_code} body={r1.text}")
        body = r1.json()
        _assert(body["source"] == "live", f"source={body.get('source')}")
        _assert(len(body["holdings"]) == 28, f"holdings={len(body['holdings'])}")
        _assert(body["cached"] is False, "first live must be uncached")
        _assert(fetcher.calls == 1, f"fetcher calls after first={fetcher.calls}")

        row_sum = sum(h["current_value"] for h in body["holdings"])
        _assert(
            abs(body["summary"]["equity"]["current_value"] - row_sum) < 1e-6,
            f"summary equity value {body['summary']['equity']['current_value']} != row sum {row_sum}",
        )

        r2 = client.get("/api/portfolio/live")
        _assert(r2.status_code == 200, f"live2 status={r2.status_code}")
        body2 = r2.json()
        _assert(body2["cached"] is True, "second live must be cached")
        _assert(fetcher.calls == 1, f"fetcher must still be 1, got {fetcher.calls}")

        r3 = client.get("/api/portfolio/live", params={"refresh": "true"})
        _assert(r3.status_code == 200, f"live refresh status={r3.status_code}")
        body3 = r3.json()
        _assert(body3["cached"] is False, "refresh must bypass cache")
        _assert(fetcher.calls == 2, f"fetcher after refresh={fetcher.calls}")
    print("  ok api_live_and_cache")


def test_api_capture_and_snapshots() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fetcher = _CountingFetcher()
        client, _db = _api_client(Path(tmp), fetcher, ttl=30.0)

        # Warm cache via live so capture reuses it.
        live = client.get("/api/portfolio/live")
        _assert(live.status_code == 200, f"live before capture={live.status_code}")
        calls_after_live = fetcher.calls

        c1 = client.post("/api/portfolio/capture")
        _assert(c1.status_code == 200, f"capture1={c1.status_code} {c1.text}")
        b1 = c1.json()
        _assert(b1["saved"] is True, f"first capture saved={b1}")
        _assert(b1["snapshot_id"] == 1, f"id={b1.get('snapshot_id')}")
        _assert(
            fetcher.calls == calls_after_live,
            f"capture must reuse cache; calls={fetcher.calls}",
        )

        c2 = client.post("/api/portfolio/capture")
        _assert(c2.status_code == 200, f"capture2={c2.status_code}")
        b2 = c2.json()
        _assert(b2["saved"] is False, f"second capture must not save: {b2}")
        _assert(b2["snapshot_id"] == 1, "second points at existing")

        # Quantity change via override + refresh so capture sees new view.
        mutated = _mutate_raw(load_from_sample(latest_sample_dir()))
        mutated.holdings[0]["quantity"] = int(mutated.holdings[0].get("quantity") or 0) + 3
        mutated.fetched_at = datetime.now(IST)
        fetcher.set_override(mutated)
        client.get("/api/portfolio/live", params={"refresh": "true"})
        c3 = client.post("/api/portfolio/capture")
        _assert(c3.status_code == 200, f"capture3={c3.status_code} {c3.text}")
        b3 = c3.json()
        _assert(b3["saved"] is True, f"qty change must save: {b3}")
        _assert(b3["snapshot_id"] == 2, f"id={b3.get('snapshot_id')}")
        _assert(b3["diff"]["changed"], f"diff.changed empty: {b3['diff']}")

        listing = client.get("/api/portfolio/snapshots")
        _assert(listing.status_code == 200, f"list={listing.status_code}")
        snaps = listing.json()["snapshots"]
        _assert(len(snaps) == 2, f"expected 2 snapshots, got {len(snaps)}")

        one = client.get("/api/portfolio/snapshots/1")
        _assert(one.status_code == 200, f"get1={one.status_code}")
        body = one.json()
        for key in ("as_of", "source", "user", "summary", "holdings", "mf_holdings", "positions"):
            _assert(key in body, f"snapshot missing {key}")
        _assert("composition" in body, "snapshot missing composition")
        _assert(body["source"] == "snapshot", f"source={body['source']}")
        _assert(len(body["holdings"]) == 28, f"holdings={len(body['holdings'])}")

        missing = client.get("/api/portfolio/snapshots/999")
        _assert(missing.status_code == 404, f"expected 404, got {missing.status_code}")
        err = missing.json()
        _assert(err.get("error") == "snapshot_not_found", f"error={err}")
    print("  ok api_capture_and_snapshots")


def test_api_token_invalid() -> None:
    from .fetch import TokenInvalid

    def bad_fetcher() -> RawPayload:
        raise TokenInvalid()

    with tempfile.TemporaryDirectory() as tmp:
        from fastapi.testclient import TestClient

        from .api import create_app

        app = create_app(
            db_path=Path(tmp) / "t.duckdb",
            fetcher=bad_fetcher,
            ui_dist=Path(tmp) / "no-ui-dist",
        )
        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/api/portfolio/live")
        _assert(r.status_code == 401, f"expected 401, got {r.status_code} {r.text}")
        body = r.json()
        _assert(body.get("error") == "token_invalid", f"error={body}")
        steps = body.get("steps") or []
        _assert(len(steps) >= 1, f"steps empty: {body}")
        _assert(any("kitetools login" in s for s in steps), f"login hint missing: {steps}")
    print("  ok api_token_invalid")


def main() -> int:
    print(f"portfolio.selftest  samples={SAMPLES_DIR}  data={DATA_DIR}")
    tests = [
        test_normalize_latest,
        test_position_enrichment,
        test_schema_migrate_v1_to_v2,
        test_fingerprint_stable_across_samples,
        test_price_change_ignored_qty_change_triggers,
        test_zero_positions_excluded_and_cnc_merge,
        test_mf_rounding_and_change,
        test_capture_idempotence,
        test_api_health,
        test_api_spa_mount,
        test_api_live_and_cache,
        test_api_capture_and_snapshots,
        test_api_token_invalid,
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
