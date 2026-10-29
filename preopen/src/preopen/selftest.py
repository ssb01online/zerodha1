"""Offline self-test for preopen capture/read (no live Kite)."""

from __future__ import annotations

import random
import tempfile
from datetime import date, datetime, time as dt_time
from pathlib import Path
from typing import Any, Callable

from instruments.models import Instrument

from .capture import (
    CaptureRejected,
    CaptureResult,
    estimate_dry_run,
    parse_at,
    run_capture,
    take_snapshot,
    wait_until,
)
from .client import KiteClient
from .read import list_days_payload, load_day
from .universe import EquityRow, list_equity


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += max(0.0, seconds)


class FakeKite:
    def __init__(self, handler: Callable[[list[str]], dict[str, Any]]) -> None:
        self.handler = handler
        self.calls: list[list[str]] = []

    def quote(self, instruments: list[str]) -> dict[str, Any]:
        self.calls.append(list(instruments))
        return self.handler(instruments)


def _inst(
    token: int,
    exchange: str,
    symbol: str,
    *,
    name: str = "",
    itype: str = "EQ",
) -> Instrument:
    return Instrument(
        instrument_token=token,
        exchange_token=token,
        tradingsymbol=symbol,
        name=name or symbol,
        last_price=0.0,
        expiry=None,
        strike=0.0,
        tick_size=0.05,
        lot_size=1,
        instrument_type=itype,
        segment=exchange,
        exchange=exchange,
    )


def _quote_payload(
    token: int,
    *,
    last_price: float,
    volume: int,
    average_price: float,
    open_: float | None = None,
    trade_day: date | None = None,
) -> dict[str, Any]:
    day = trade_day or date(2026, 10, 29)
    ts = f"{day.isoformat()} 09:08:45"
    open_px = open_ if open_ is not None else last_price
    return {
        "instrument_token": token,
        "timestamp": ts,
        "last_trade_time": ts,
        "last_price": last_price,
        "volume": volume,
        "average_price": average_price,
        "buy_quantity": 100,
        "sell_quantity": 50,
        "ohlc": {
            "open": open_px,
            "high": open_px,
            "low": open_px,
            "close": last_price - 1,
        },
    }


def test_parse_at() -> None:
    _assert(parse_at("09:08:30") == dt_time(9, 8, 30), "full")
    _assert(parse_at("9:08") == dt_time(9, 8, 0), "short")
    try:
        parse_at("25:00")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_wait_until_past_is_noop() -> None:
    from kitetools import auth

    sleeps: list[float] = []
    now = datetime(2026, 10, 29, 9, 10, 0, tzinfo=auth.IST)
    out = wait_until(
        dt_time(9, 8, 30),
        now=now,
        sleep=lambda s: sleeps.append(s),
    )
    _assert(sleeps == [], f"slept {sleeps}")
    _assert(out == now, "returns now")


def test_wait_estimate() -> None:
    from kitetools import auth

    now = datetime(2026, 10, 29, 9, 8, 28, tzinfo=auth.IST)
    est = estimate_dry_run(
        [],
        at=dt_time(9, 8, 30),
        now=now,
        skip_wait=False,
    )
    _assert(abs(est.wait_seconds - 2.0) < 0.01, f"wait={est.wait_seconds}")
    _assert(est.wait_until is not None, "has wait_until")


def test_universe_filters_eq_only() -> None:
    rows = [
        _inst(1, "NSE", "INFY", name="INFOSYS"),
        _inst(2, "BSE", "500325", name="RELIANCE"),
        _inst(3, "NFO", "NIFTYFUT", itype="FUT"),
        _inst(4, "NSE", "NIFTY 50", itype="INDEX"),
        _inst(5, "NSE", "RELIANCE", name="RELIANCE"),
    ]
    eq = list_equity(rows)
    _assert(len(eq) == 3, f"len={len(eq)}")
    _assert([e.tradingsymbol for e in eq] == ["500325", "INFY", "RELIANCE"], "sorted")
    _assert(eq[1].quote_key == "NSE:INFY", "key")


def test_capture_batches_and_traded_value() -> None:
    from kitetools import auth

    equities = [
        EquityRow(token=10, exchange="NSE", tradingsymbol="AAA", name="A"),
        EquityRow(token=11, exchange="NSE", tradingsymbol="BBB", name="B"),
        EquityRow(token=12, exchange="BSE", tradingsymbol="CCC", name="C"),
    ]
    # Force 2 batches with batch size monkeypatch
    import preopen.capture as cap_mod

    old = cap_mod.QUOTE_BATCH_SIZE
    cap_mod.QUOTE_BATCH_SIZE = 2
    try:
        trade_day = date(2026, 10, 29)

        def handler(keys: list[str]) -> dict[str, Any]:
            out: dict[str, Any] = {}
            for key in keys:
                if key.endswith("AAA"):
                    out[key] = _quote_payload(
                        10, last_price=100, volume=1000, average_price=101.5, trade_day=trade_day
                    )
                elif key.endswith("BBB"):
                    out[key] = _quote_payload(
                        11, last_price=50, volume=0, average_price=0, trade_day=trade_day
                    )
                elif key.endswith("CCC"):
                    out[key] = _quote_payload(
                        12, last_price=200, volume=500, average_price=199.0, trade_day=trade_day
                    )
            return out

        kite = FakeKite(handler)
        clock = FakeClock()
        client = KiteClient(
            kite,  # type: ignore[arg-type]
            quote_interval=0.0,
            clock=clock,  # type: ignore[arg-type]
            rng=random.Random(0),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "preopen"
            staging = root / "staging"
            catalog = root / "catalog.duckdb"
            now = datetime(2026, 10, 29, 9, 8, 45, tzinfo=auth.IST)
            result = take_snapshot(
                client,
                equities,
                now=now,
                dest_dir=dest,
                staging_dir=staging,
                catalog_path=catalog,
            )
            _assert(isinstance(result, CaptureResult), "result type")
            _assert(result.equities == 3, f"rows={result.equities}")
            _assert(result.traded_rows == 2, f"traded={result.traded_rows}")
            _assert(result.missing == 0, "missing")
            expected_tv = 1000 * 101.5 + 500 * 199.0
            _assert(
                abs(result.total_traded_value - expected_tv) < 1e-6,
                f"tv={result.total_traded_value}",
            )
            _assert(result.path.is_file(), "parquet exists")
            _assert(len(kite.calls) == 2, f"batches={len(kite.calls)}")

            days = list_days_payload(catalog_path=catalog)
            _assert(days["count"] == 1, f"days={days}")
            payload = load_day(
                trade_day,
                limit=10,
                dest_dir=dest,
                catalog_path=catalog,
            )
            _assert(payload["items"][0]["tradingsymbol"] == "AAA", "top symbol")
            _assert(
                abs(payload["day_total_traded_value"] - expected_tv) < 1e-6,
                "day total",
            )
            nse = load_day(
                trade_day,
                limit=10,
                exchange="NSE",
                dest_dir=dest,
                catalog_path=catalog,
            )
            _assert(nse["filtered_rows"] == 2, f"nse filtered={nse['filtered_rows']}")
            # AAA is only traded NSE; BBB volume 0 still counts in filtered if min_value=0
            _assert(len(nse["items"]) == 2, "nse items")
    finally:
        cap_mod.QUOTE_BATCH_SIZE = old


def test_capture_rejects_zero_volume() -> None:
    from kitetools import auth

    equities = [EquityRow(token=1, exchange="NSE", tradingsymbol="Z", name="Z")]

    def handler(keys: list[str]) -> dict[str, Any]:
        return {
            keys[0]: _quote_payload(1, last_price=10, volume=0, average_price=0)
        }

    kite = FakeKite(handler)
    client = KiteClient(
        kite,  # type: ignore[arg-type]
        quote_interval=0.0,
        clock=FakeClock(),  # type: ignore[arg-type]
        rng=random.Random(0),
    )
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        try:
            take_snapshot(
                client,
                equities,
                now=datetime(2026, 10, 29, 9, 8, 45, tzinfo=auth.IST),
                dest_dir=root / "preopen",
                staging_dir=root / "staging",
                catalog_path=root / "catalog.duckdb",
            )
            raise AssertionError("expected CaptureRejected")
        except CaptureRejected as exc:
            _assert("no pre-open volume" in str(exc), str(exc))


def test_run_capture_force_skips_wait() -> None:
    from kitetools import auth

    equities = [
        EquityRow(token=7, exchange="NSE", tradingsymbol="INFY", name="INFY"),
    ]
    slept: list[float] = []

    def handler(keys: list[str]) -> dict[str, Any]:
        return {
            keys[0]: _quote_payload(
                7, last_price=1400, volume=100, average_price=1401.0
            )
        }

    kite = FakeKite(handler)
    client = KiteClient(
        kite,  # type: ignore[arg-type]
        quote_interval=0.0,
        clock=FakeClock(),  # type: ignore[arg-type]
        rng=random.Random(0),
    )
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        now = datetime(2026, 10, 29, 8, 50, 0, tzinfo=auth.IST)
        result = run_capture(
            client,
            at=dt_time(9, 8, 30),
            skip_wait=True,
            now=now,
            dest_dir=root / "preopen",
            staging_dir=root / "staging",
            catalog_path=root / "catalog.duckdb",
            equities=equities,
            sleep=lambda s: slept.append(s),
        )
        _assert(isinstance(result, CaptureResult), "result")
        _assert(slept == [], f"slept={slept}")
        _assert(result.traded_rows == 1, "traded")


def test_dry_run() -> None:
    from kitetools import auth

    equities = [
        EquityRow(token=i, exchange="NSE", tradingsymbol=f"S{i}", name=f"S{i}")
        for i in range(501)
    ]
    now = datetime(2026, 10, 29, 8, 50, 0, tzinfo=auth.IST)
    est = estimate_dry_run(equities, at=dt_time(9, 8, 30), now=now)
    _assert(est.equities == 501, "eq")
    _assert(est.batches == 2, f"batches={est.batches}")
    _assert(est.estimated_seconds == 2.0, "est sec")
    _assert(est.wait_seconds > 0, "wait")


def test_api_router_offline() -> None:
    from fastapi.testclient import TestClient
    from kitetools import auth

    from .api import router
    from fastapi import FastAPI

    equities = [
        EquityRow(token=1, exchange="NSE", tradingsymbol="INFY", name="INFY"),
        EquityRow(token=2, exchange="BSE", tradingsymbol="500325", name="RIL"),
    ]

    def handler(keys: list[str]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key in keys:
            if "INFY" in key:
                out[key] = _quote_payload(
                    1, last_price=100, volume=1000, average_price=100
                )
            else:
                out[key] = _quote_payload(
                    2, last_price=50, volume=200, average_price=50
                )
        return out

    kite = FakeKite(handler)
    client = KiteClient(
        kite,  # type: ignore[arg-type]
        quote_interval=0.0,
        clock=FakeClock(),  # type: ignore[arg-type]
        rng=random.Random(0),
    )
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        dest = root / "preopen"
        catalog = root / "catalog.duckdb"
        take_snapshot(
            client,
            equities,
            now=datetime(2026, 10, 29, 9, 8, 45, tzinfo=auth.IST),
            dest_dir=dest,
            staging_dir=root / "staging",
            catalog_path=catalog,
        )

        # Point module defaults at temp paths for API.
        import preopen.catalog as catalog_mod
        import preopen.paths as paths_mod

        old_preopen = paths_mod.PREOPEN_DIR
        old_catalog = paths_mod.CATALOG_PATH
        paths_mod.PREOPEN_DIR = dest
        paths_mod.CATALOG_PATH = catalog
        catalog_mod.CATALOG_PATH = catalog
        try:
            app = FastAPI()
            app.include_router(router)
            tc = TestClient(app)
            days = tc.get("/api/preopen/days")
            _assert(days.status_code == 200, days.text)
            body = days.json()
            _assert(body["count"] == 1, body)
            day = tc.get("/api/preopen/day/20261029?limit=1")
            _assert(day.status_code == 200, day.text)
            dbody = day.json()
            _assert(len(dbody["items"]) == 1, dbody)
            _assert(dbody["items"][0]["tradingsymbol"] == "INFY", dbody)
            missing = tc.get("/api/preopen/day/20260101")
            _assert(missing.status_code == 404, missing.text)
        finally:
            paths_mod.PREOPEN_DIR = old_preopen
            paths_mod.CATALOG_PATH = old_catalog
            catalog_mod.CATALOG_PATH = old_catalog


def main() -> None:
    tests = [
        test_parse_at,
        test_wait_until_past_is_noop,
        test_wait_estimate,
        test_universe_filters_eq_only,
        test_capture_batches_and_traded_value,
        test_capture_rejects_zero_volume,
        test_run_capture_force_skips_wait,
        test_dry_run,
        test_api_router_offline,
    ]
    for fn in tests:
        print(f"  {fn.__name__}…", flush=True)
        fn()
    print(f"preopen selftest: {len(tests)} ok")


if __name__ == "__main__":
    main()
