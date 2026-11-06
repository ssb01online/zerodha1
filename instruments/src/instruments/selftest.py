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
from .scope import IN_SCOPE_EXCHANGES, in_scope


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
    exchange_token: int | None = None,
) -> Instrument:
    return Instrument(
        instrument_token=token,
        exchange_token=token // 100 if exchange_token is None else exchange_token,
        tradingsymbol=symbol,
        name=name,
        last_price=0.0,
        expiry=expiry,
        strike=strike,
        tick_size=0.05,
        lot_size=lot_size,
        instrument_type=itype,
        segment=(
            f"{exchange}-{itype}"
            if exchange in ("NFO", "MCX", "CDS") and itype in ("FUT", "CE", "PE")
            else exchange
        ),
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
        _inst(
            30,
            exchange="MCX",
            symbol="GOLD26DECFUT",
            name="GOLD",
            itype="FUT",
            expiry=date(2026, 12, 4),
            lot_size=1,
        ),
        _inst(
            40,
            exchange="CDS",
            symbol="USDINR26OCTFUT",
            name="USDINR",
            itype="FUT",
            expiry=exp,
            lot_size=1,
        ),
    ]


def test_equity_publish_filter_and_history() -> None:
    """EQ cash rows only; indices dropped; first_seen, delist, and rename stick."""
    from .equity import get_equity, is_cash_equity, list_equity, publish_equity

    day1 = date(2026, 10, 27)
    day2 = date(2026, 10, 28)
    infy = _inst(1, exchange="NSE", symbol="INFY", name="INFOSYS")
    reliance = _inst(3, exchange="NSE", symbol="RELIANCE", name="RELIANCE INDUSTRIES")
    bse = _inst(
        2,
        exchange="BSE",
        symbol="INFY",
        name="INFOSYS",
        exchange_token=500209,
    )
    index = Instrument(
        instrument_token=256265,
        exchange_token=1001,
        tradingsymbol="NIFTY 50",
        name="NIFTY 50",
        last_price=0.0,
        expiry=None,
        strike=0.0,
        tick_size=0.0,
        lot_size=0,
        instrument_type="EQ",
        segment="INDICES",
        exchange="NSE",
    )
    be_type = _inst(9, exchange="NSE", symbol="FOO", name="FOO", itype="BE")
    t2t = _inst(4, exchange="NSE", symbol="ABAN-BE", name="ABAN OFFSHORE")
    sme = _inst(5, exchange="NSE", symbol="AAKAAR-SM", name="AAKAAR MEDICAL")
    hyphen_eq = _inst(6, exchange="NSE", symbol="BAJAJ-AUTO", name="BAJAJ AUTO")
    debt = _inst(10, exchange="NSE", symbol="10157PCP27-N0", name="")
    sdl = _inst(13, exchange="NSE", symbol="582RJ27-SG", name="SDL RJ 5.82% 2027")
    gold = _inst(14, exchange="NSE", symbol="SGBAPR28I-GB", name="GOLDBONDS")
    invit = _inst(15, exchange="NSE", symbol="INDIGRID-IV", name="INDIGRID - INVIT")
    bse_debt = _inst(
        12,
        exchange="BSE",
        symbol="001HCCL29",
        name="",
        exchange_token=974246,
    )
    bse_mf = _inst(
        16,
        exchange="BSE",
        symbol="08ABB",
        name="NIPPON INDIA MUTUAL FUND",
        exchange_token=543076,
    )
    bse_etf = _inst(
        17,
        exchange="BSE",
        symbol="SBISENSEX",
        name="SBI MUTUAL FUND - SBI ETF SENS",
        exchange_token=590105,
    )
    _assert(is_cash_equity(infy) and is_cash_equity(bse), "eq included")
    _assert(is_cash_equity(t2t) and is_cash_equity(sme), "t2t and sme included")
    _assert(is_cash_equity(hyphen_eq), "hyphenated EQ symbol included")
    _assert(is_cash_equity(bse_etf), "bse etf included")
    _assert(not is_cash_equity(index), "index excluded")
    _assert(not is_cash_equity(be_type), "non-EQ instrument type excluded")
    _assert(not is_cash_equity(debt), "nse N-series debt excluded")
    _assert(not is_cash_equity(sdl) and not is_cash_equity(gold), "sdl and gold excluded")
    _assert(not is_cash_equity(invit), "invit excluded")
    _assert(not is_cash_equity(bse_debt), "bse debt scrip excluded")
    _assert(not is_cash_equity(bse_mf), "bse mutual fund excluded")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db = root / "equity.duckdb"
        csv_path = root / "equity.csv"
        first = publish_equity(
            [infy, reliance, bse, index, be_type, t2t, debt, bse_debt, bse_mf],
            as_of=day1,
            db_path=db,
            csv_path=csv_path,
        )
        _assert(first.live == 4 and first.nse == 3 and first.bse == 1, f"live={first}")
        stored = {r.instrument_token: r for r in list_equity(db)}
        _assert(set(stored) == {1, 2, 3, 4}, f"tokens={set(stored)}")
        _assert(stored[1].first_seen == day1 and stored[1].prev_tradingsymbol is None, "first seen")
        text = csv_path.read_text(encoding="utf-8")
        _assert("NIFTY 50" not in text and "FOO" not in text, "csv dropped non-EQ")
        _assert("10157PCP27-N0" not in text and "001HCCL29" not in text, "csv dropped debt")
        _assert("ABAN-BE" in text, "csv kept trade-for-trade equity")
        _assert(text.count("INFY") == 2, "csv has both INFY rows")

        renamed = _inst(1, exchange="NSE", symbol="INFYNEW", name="INFOSYS")
        second = publish_equity(
            [renamed, bse],
            as_of=day2,
            db_path=db,
            csv_path=csv_path,
        )
        _assert(second.live == 2, f"day2 live={second.live}")
        kept = get_equity(db, token=3)
        _assert(kept is not None and kept.last_seen == day1, f"vanished={kept}")
        again = get_equity(db, token=1)
        _assert(again is not None, "renamed missing")
        _assert(again.first_seen == day1 and again.last_seen == day2, f"dates={again}")
        _assert(again.tradingsymbol == "INFYNEW", "new symbol")
        _assert(again.prev_tradingsymbol == "INFY", f"prev={again.prev_tradingsymbol}")
        live_csv = csv_path.read_text(encoding="utf-8")
        _assert("RELIANCE" not in live_csv, "csv is live universe only")
        _assert("INFYNEW" in live_csv and "INFY," in live_csv, "csv has rename and BSE")

        # Same calendar day: a name already stamped today must leave the live set
        # when this publish does not accept it (filter change, or it left the dump).
        same_day = publish_equity(
            [renamed],
            as_of=day2,
            db_path=db,
            csv_path=csv_path,
        )
        _assert(same_day.live == 1 and same_day.nse == 1 and same_day.bse == 0, f"same-day={same_day}")
        dropped_bse = get_equity(db, token=2)
        _assert(
            dropped_bse is not None and dropped_bse.last_seen == day1,
            f"same-day last_seen={dropped_bse}",
        )
        fresh = _inst(8, exchange="NSE", symbol="NEWCO", name="NEWCO")
        publish_equity([renamed, fresh], as_of=day2, db_path=db, csv_path=csv_path)
        publish_equity([renamed], as_of=day2, db_path=db, csv_path=csv_path)
        gone = get_equity(db, token=8)
        _assert(gone is not None and gone.first_seen == day1 and gone.last_seen == day1, f"only-today={gone}")
        after = csv_path.read_text(encoding="utf-8")
        _assert("NEWCO" not in after and ",BSE,INFY," not in after, "same-day csv dropped them")
    print("  ok equity_publish_filter_and_history")


def test_scope_includes_mcx_cds() -> None:
    _assert(IN_SCOPE_EXCHANGES == frozenset({"NSE", "BSE", "NFO", "MCX", "CDS"}), "scope set")
    _assert(in_scope("MCX") and in_scope("mcx"), "mcx in scope")
    _assert(in_scope("CDS") and in_scope("cds"), "cds in scope")
    _assert(not in_scope("BFO"), "bfo still out of scope")
    _assert(not in_scope(None), "none out of scope")
    print("  ok scope_includes_mcx_cds")


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
        _assert(m["total"] == 10, f"meta total={m['total']}")
        _assert(m["by_exchange"].get("NFO") == 5, f"nfo count={m['by_exchange']}")
        _assert(m["by_exchange"].get("MCX") == 1, f"mcx count={m['by_exchange']}")
        _assert(m["by_exchange"].get("CDS") == 1, f"cds count={m['by_exchange']}")
        mcx = query.search(q="GOLD", exchange="MCX")
        _assert(mcx["total"] == 1, f"mcx gold={mcx['total']}")
        cds = query.search(q="USDINR", exchange="CDS")
        _assert(cds["total"] == 1, f"cds usdinr={cds['total']}")
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
        _assert(r.json()["total"] == 10, f"meta body={r.json()}")

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
            _assert(r.json()["total"] == 10, r.json())
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
        test_equity_publish_filter_and_history,
        test_scope_includes_mcx_cds,
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
