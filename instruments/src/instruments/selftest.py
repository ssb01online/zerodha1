"""Offline self-tests for instruments query/API. No Kite login required.

Run: uv run python -m instruments.selftest
"""

from __future__ import annotations

import sys
import tempfile
from datetime import date
from pathlib import Path

from . import query
from .cache import clear_rows_override, set_rows_override
from .models import Instrument


class Failure(Exception):
    pass


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise Failure(msg)


def _inst(
    token: int,
    *,
    exchange: str,
    symbol: str,
    name: str = "",
    itype: str = "EQ",
    expiry: date | None = None,
    strike: float = 0.0,
    lot_size: int = 1,
) -> Instrument:
    return Instrument(
        instrument_token=token,
        exchange_token=token // 100,
        tradingsymbol=symbol,
        name=name,
        last_price=0.0,
        expiry=expiry,
        strike=strike,
        tick_size=0.05,
        lot_size=lot_size,
        instrument_type=itype,
        segment=exchange if exchange != "NFO" else f"NFO-{itype}",
        exchange=exchange,
    )


def _fixture_rows() -> list[Instrument]:
    exp = date(2026, 10, 29)
    return [
        _inst(1, exchange="NSE", symbol="INFY", name="INFOSYS", itype="EQ"),
        _inst(2, exchange="BSE", symbol="INFY", name="INFOSYS", itype="EQ"),
        _inst(3, exchange="NSE", symbol="RELIANCE", name="RELIANCE", itype="EQ"),
        # Out-of-scope exchange would be filtered at ingest; include only in-scope here.
        _inst(
            10,
            exchange="NFO",
            symbol="NIFTY26OCTFUT",
            name="NIFTY",
            itype="FUT",
            expiry=exp,
            lot_size=75,
        ),
        _inst(
            11,
            exchange="NFO",
            symbol="NIFTY26OCT24000CE",
            name="NIFTY",
            itype="CE",
            expiry=exp,
            strike=24000,
            lot_size=75,
        ),
        _inst(
            12,
            exchange="NFO",
            symbol="NIFTY26OCT24000PE",
            name="NIFTY",
            itype="PE",
            expiry=exp,
            strike=24000,
            lot_size=75,
        ),
        _inst(
            13,
            exchange="NFO",
            symbol="NIFTY26OCT24100CE",
            name="NIFTY",
            itype="CE",
            expiry=exp,
            strike=24100,
            lot_size=75,
        ),
        _inst(
            20,
            exchange="NFO",
            symbol="TCS26OCTFUT",
            name="TCS",
            itype="FUT",
            expiry=exp,
            lot_size=175,
        ),
    ]


def test_search_pagination_and_filters() -> None:
    set_rows_override(_fixture_rows())
    query.invalidate_indexes()
    try:
        all_eq = query.search(instrument_type="EQ", limit=10, offset=0)
        _assert(all_eq["total"] == 3, f"eq total={all_eq['total']}")
        page = query.search(instrument_type="EQ", limit=2, offset=0)
        _assert(len(page["items"]) == 2, f"page len={len(page['items'])}")
        page2 = query.search(instrument_type="EQ", limit=2, offset=2)
        _assert(len(page2["items"]) == 1, f"page2 len={len(page2['items'])}")
        nse = query.search(q="INFY", exchange="NSE")
        _assert(nse["total"] == 1, f"nse infy={nse['total']}")
        _assert(nse["items"][0]["instrument_token"] == 1, "wrong token")
        m = query.meta()
        _assert(m["total"] == 8, f"meta total={m['total']}")
        _assert(m["by_exchange"].get("NFO") == 5, f"nfo count={m['by_exchange']}")
    finally:
        clear_rows_override()
        query.invalidate_indexes()
    print("  ok search_pagination_and_filters")


def test_by_token_and_chain() -> None:
    set_rows_override(_fixture_rows())
    query.invalidate_indexes()
    try:
        got = query.get(11)
        _assert(got is not None and got.tradingsymbol.endswith("CE"), "ce missing")
        _assert(query.get(999) is None, "expected miss")

        und = query.underlyings(q="NIF")
        names = [u["name"] for u in und["items"]]
        _assert("NIFTY" in names, f"underlyings={names}")

        exps = query.expiries("NIFTY")
        _assert(exps["expiries"] == ["2026-10-29"], f"expiries={exps}")

        ch = query.chain("NIFTY", "2026-10-29")
        _assert(ch["fut"] is not None, "fut missing")
        _assert(ch["fut"]["instrument_token"] == 10, "fut token")
        _assert(len(ch["strikes"]) == 2, f"strikes={len(ch['strikes'])}")
        s0 = ch["strikes"][0]
        _assert(s0["strike"] == 24000, f"strike0={s0}")
        _assert(s0["ce"]["instrument_token"] == 11, "ce token")
        _assert(s0["pe"]["instrument_token"] == 12, "pe token")
        s1 = ch["strikes"][1]
        _assert("ce" in s1 and "pe" not in s1, "24100 should be CE-only")
    finally:
        clear_rows_override()
        query.invalidate_indexes()
    print("  ok by_token_and_chain")


def test_api_routes() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from .api import router

    set_rows_override(_fixture_rows())
    query.invalidate_indexes()
    try:
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        r = client.get("/api/instruments/meta")
        _assert(r.status_code == 200, f"meta={r.status_code}")
        _assert(r.json()["total"] == 8, f"meta body={r.json()}")

        r = client.get("/api/instruments/search", params={"q": "INFY", "limit": 10})
        _assert(r.status_code == 200, f"search={r.status_code}")
        _assert(r.json()["total"] == 2, f"search total={r.json()}")

        r = client.get("/api/instruments/by-token/11")
        _assert(r.status_code == 200, f"by-token={r.status_code}")
        _assert(r.json()["instrument_type"] == "CE", r.json())

        r = client.get("/api/instruments/by-token/999")
        _assert(r.status_code == 404, f"expected 404 got {r.status_code}")
        _assert(r.json().get("error") == "instrument_not_found", r.json())

        r = client.get("/api/instruments/nfo/underlyings", params={"q": "TCS"})
        _assert(r.status_code == 200 and r.json()["total"] >= 1, r.text)

        r = client.get(
            "/api/instruments/nfo/chain",
            params={"underlying": "NIFTY", "expiry": "2026-10-29"},
        )
        _assert(r.status_code == 200, f"chain={r.status_code}")
        body = r.json()
        _assert(body["fut"]["lot_size"] == 75, body)
        _assert(len(body["strikes"]) == 2, body)
    finally:
        clear_rows_override()
        query.invalidate_indexes()
    print("  ok api_routes")


def test_mounted_on_portfolio_app() -> None:
    from fastapi.testclient import TestClient

    set_rows_override(_fixture_rows())
    query.invalidate_indexes()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            from portfolio.api import create_app

            app = create_app(
                db_path=Path(tmp) / "t.duckdb",
                ui_dist=Path(tmp) / "no-ui",
            )
            client = TestClient(app, raise_server_exceptions=False)
            r = client.get("/api/instruments/meta")
            _assert(r.status_code == 200, f"mounted meta={r.status_code} {r.text}")
            _assert(r.json()["total"] == 8, r.json())
            health = client.get("/api/health")
            _assert(health.status_code == 200, health.text)
            _assert("instruments_as_of" in health.json(), health.json())
    finally:
        clear_rows_override()
        query.invalidate_indexes()
    print("  ok mounted_on_portfolio_app")


def main() -> int:
    print("instruments.selftest")
    tests = [
        test_search_pagination_and_filters,
        test_by_token_and_chain,
        test_api_routes,
        test_mounted_on_portfolio_app,
    ]
    failed = 0
    for test in tests:
        try:
            test()
        except Failure as exc:
            print(f"  FAIL {test.__name__}: {exc}")
            failed += 1
        except Exception as exc:
            print(f"  ERROR {test.__name__}: {type(exc).__name__}: {exc}")
            failed += 1
    if failed:
        print(f"{failed}/{len(tests)} failed")
        return 1
    print(f"All {len(tests)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
