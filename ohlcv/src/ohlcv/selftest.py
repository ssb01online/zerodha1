"""Offline self-tests for ohlcv Phase 1. No Kite login required.

Run: uv run python -m ohlcv.selftest
"""

from __future__ import annotations

import json
import random
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from instruments.cache import clear_rows_override, set_rows_override
from instruments.models import Instrument
from kiteconnect.exceptions import InputException, NetworkException, TokenException

from . import archive as archive_mod
from . import catalog
from . import planner
from . import universe
from .client import (
    KiteClient,
    RequestRejected,
    RetryExhausted,
    SessionExpired,
)
from .paths import sanitize_underlying, ymd


class Failure(Exception):
    pass


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise Failure(msg)


def _inst(
    token: int,
    *,
    symbol: str,
    name: str,
    itype: str = "FUT",
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
        segment=f"NFO-{itype}",
        exchange="NFO",
    )


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += max(0.0, seconds)


class FakeKite:
    def __init__(self, handler: Callable[..., Any] | None = None) -> None:
        self.handler = handler
        self.calls = 0

    def historical_data(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls += 1
        if self.handler is not None:
            return self.handler(*args, **kwargs)
        return []

    def quote(self, instruments: list[str]) -> dict[str, Any]:
        self.calls += 1
        if self.handler is not None:
            return self.handler(instruments)
        return {}


def test_paths() -> None:
    _assert(ymd(date(2026, 10, 27)) == "20261027", "ymd date")
    _assert(ymd(datetime(2026, 1, 2, 15, 30)) == "20260102", "ymd datetime")
    _assert(sanitize_underlying("M&M") == "M_M", "sanitize M&M")
    _assert(sanitize_underlying("BAJAJ-AUTO") == "BAJAJ-AUTO", "sanitize hyphen")
    _assert(sanitize_underlying("  nifty ") == "NIFTY", "sanitize strip/upper")


def test_archive_idempotent() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        master = root / "master.json"
        archive_dir = root / "archive"
        payload = {
            "as_of_date": "2026-10-25",
            "rows": [{"instrument_token": 1}],
        }
        master.write_text(json.dumps(payload), encoding="utf-8")
        p1 = archive_mod.ensure_archive(master_path=master, archive_dir=archive_dir)
        _assert(p1.name == "20261025.json.gz", f"archive name={p1.name}")
        _assert(p1.is_file(), "archive exists")
        mtime1 = p1.stat().st_mtime_ns
        p2 = archive_mod.ensure_archive(master_path=master, archive_dir=archive_dir)
        _assert(p1 == p2, "same path")
        _assert(p2.stat().st_mtime_ns == mtime1, "idempotent (not rewritten)")


def test_catalog_schema() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "c.duckdb"
        con = catalog.connect(db)
        try:
            catalog.ensure_schema(con)
            _assert(catalog.schema_version(con) == catalog.SCHEMA_VERSION, "schema version")
            catalog.ensure_schema(con)  # idempotent
            today = date(2026, 10, 25)
            n = catalog.upsert_contracts(
                con,
                [
                    catalog.ContractRow(
                        token=1,
                        underlying="NIFTY",
                        expiry=date(2026, 10, 27),
                        tradingsymbol="NIFTY26OCTFUT",
                        instrument_type="FUT",
                        strike=0.0,
                        lot_size=75,
                    )
                ],
                seen_on=today,
            )
            _assert(n == 1, "upsert count")
            _assert(catalog.contract_count(con) == 1, "contract count")
            rows = catalog.list_contracts(con, underlying="NIFTY")
            _assert(len(rows) == 1 and rows[0].first_seen == today, "first_seen")
            catalog.upsert_contracts(
                con,
                [
                    catalog.ContractRow(
                        token=1,
                        underlying="NIFTY",
                        expiry=date(2026, 10, 27),
                        tradingsymbol="NIFTY26OCTFUT",
                        instrument_type="FUT",
                        strike=0.0,
                        lot_size=75,
                    )
                ],
                seen_on=date(2026, 10, 26),
            )
            rows = catalog.list_contracts(con)
            _assert(rows[0].first_seen == today, "first_seen preserved")
            _assert(rows[0].last_seen == date(2026, 10, 26), "last_seen updated")
        finally:
            con.close()


def test_limiter_spacing() -> None:
    clock = FakeClock()
    kite = FakeKite(handler=lambda *a, **k: [])
    client = KiteClient(
        kite,  # type: ignore[arg-type]
        hist_interval=0.34,
        clock=clock,
        rng=random.Random(0),
    )
    client.historical(1, "2026-10-01", "2026-10-02", "day")
    client.historical(1, "2026-10-01", "2026-10-02", "day")
    _assert(len(clock.sleeps) >= 1, "slept between hist calls")
    _assert(clock.sleeps[0] >= 0.33, f"hist spacing={clock.sleeps[0]}")


def test_retry_ladder_and_caps() -> None:
    clock = FakeClock()
    attempts = {"n": 0}

    def boom(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        attempts["n"] += 1
        raise NetworkException("temporary")

    kite = FakeKite(handler=boom)
    client = KiteClient(
        kite,  # type: ignore[arg-type]
        hist_interval=0.0,
        clock=clock,
        rng=random.Random(0),  # deterministic jitter
    )
    try:
        client.historical(42, "2026-10-01", "2026-10-02", "day")
        raise Failure("expected RetryExhausted")
    except RetryExhausted as exc:
        _assert(exc.token == 42, "token on RetryExhausted")
    _assert(attempts["n"] == 6, f"attempts={attempts['n']}")
    _assert(client.counters.retries == 5, f"retries={client.counters.retries}")
    _assert(client.counters.errors == 1, "errors=1")
    # 5 backoff sleeps (between attempts); each base capped and jittered.
    _assert(len(clock.sleeps) == 5, f"backoff sleeps={len(clock.sleeps)}")
    _assert(max(clock.sleeps) <= 60.0 * 1.2 + 0.01, f"cap violated: {max(clock.sleeps)}")


def test_rate_limit_min_wait() -> None:
    clock = FakeClock()
    attempts = {"n": 0}

    def boom(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise NetworkException("HTTP 429 Too Many Requests")
        return [{"date": "2026-10-01", "close": 1}]

    kite = FakeKite(handler=boom)
    client = KiteClient(
        kite,  # type: ignore[arg-type]
        hist_interval=0.0,
        clock=clock,
        rng=random.Random(0),
    )
    rows = client.historical(1, "2026-10-01", "2026-10-02", "day")
    _assert(len(rows) == 1, "recovered after 429")
    _assert(clock.sleeps[0] >= 5.0, f"429 min wait={clock.sleeps[0]}")


def test_input_exception_not_retried() -> None:
    clock = FakeClock()
    attempts = {"n": 0}

    def boom(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        attempts["n"] += 1
        raise InputException("bad range")

    kite = FakeKite(handler=boom)
    client = KiteClient(
        kite,  # type: ignore[arg-type]
        hist_interval=0.0,
        clock=clock,
        rng=random.Random(0),
    )
    try:
        client.historical(1, "2026-10-01", "2026-10-02", "day")
        raise Failure("expected RequestRejected")
    except RequestRejected:
        pass
    _assert(attempts["n"] == 1, "InputException not retried")
    _assert(client.counters.retries == 0, "no retries")


def test_token_exception_session_expired() -> None:
    clock = FakeClock()

    def boom(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        raise TokenException("invalid")

    kite = FakeKite(handler=boom)
    client = KiteClient(
        kite,  # type: ignore[arg-type]
        hist_interval=0.0,
        clock=clock,
        rng=random.Random(0),
    )
    try:
        client.historical(1, "2026-10-01", "2026-10-02", "day")
        raise Failure("expected SessionExpired")
    except SessionExpired:
        pass


def _seed_universe(con: Any, today: date) -> None:
    """Insert multi-expiry NFO contracts for nearest-3 + unfinished expired tests."""
    # Use catalog upsert directly (no instruments override needed for select_series).
    rows = [
        # NIFTY: 4 live expiries → nearest 3 only
        catalog.ContractRow(1, "NIFTY", date(2026, 10, 27), "NIFTY1", "FUT", 0, 75),
        catalog.ContractRow(2, "NIFTY", date(2026, 11, 3), "NIFTY2", "FUT", 0, 75),
        catalog.ContractRow(3, "NIFTY", date(2026, 11, 10), "NIFTY3", "FUT", 0, 75),
        catalog.ContractRow(4, "NIFTY", date(2026, 11, 17), "NIFTY4", "FUT", 0, 75),
        catalog.ContractRow(5, "NIFTY", date(2026, 10, 27), "NIFTY1CE", "CE", 24000, 75),
        # RELIANCE unfinished expired (expiry < today)
        catalog.ContractRow(10, "RELIANCE", date(2026, 10, 20), "REL1", "FUT", 0, 250),
        catalog.ContractRow(11, "RELIANCE", date(2026, 10, 20), "REL1CE", "CE", 1400, 250),
        # Finished expired (should NOT appear)
        catalog.ContractRow(20, "INFY", date(2026, 10, 20), "INFY1", "FUT", 0, 400),
        # Live stock
        catalog.ContractRow(30, "RELIANCE", date(2026, 10, 27), "REL2", "FUT", 0, 250),
        catalog.ContractRow(31, "M&M", date(2026, 10, 27), "MM1", "FUT", 0, 500),
    ]
    catalog.upsert_contracts(con, rows, seen_on=today)
    # Mark INFY expired series as done through session end.
    session_end = datetime(2026, 10, 20, 15, 40, 0)
    con.execute(
        """
        INSERT INTO fetch_state (token, interval, fetched_to, status, rows_total, attempts, last_error, updated_at)
        VALUES (20, 'minute', ?, 'done', 100, 1, NULL, ?)
        """,
        [session_end, session_end],
    )


def test_universe_nearest3_and_unfinished() -> None:
    today = date(2026, 10, 25)
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "c.duckdb"
        con = catalog.connect(db)
        try:
            catalog.ensure_schema(con)
            _seed_universe(con, today)
            series = universe.select_series(con, today)
            keys = {(s.underlying, s.expiry) for s in series}
            # Nearest 3 for NIFTY — not 11-17
            _assert(("NIFTY", date(2026, 10, 27)) in keys, "nifty near")
            _assert(("NIFTY", date(2026, 11, 3)) in keys, "nifty 2")
            _assert(("NIFTY", date(2026, 11, 10)) in keys, "nifty 3")
            _assert(("NIFTY", date(2026, 11, 17)) not in keys, "4th expiry excluded")
            # Unfinished expired RELIANCE 10-20
            _assert(("RELIANCE", date(2026, 10, 20)) in keys, "unfinished expired included")
            # Finished expired INFY excluded
            _assert(("INFY", date(2026, 10, 20)) not in keys, "finished expired excluded")
            # Live RELIANCE / M&M
            _assert(("RELIANCE", date(2026, 10, 27)) in keys, "live reliance")
            _assert(("M&M", date(2026, 10, 27)) in keys, "live M&M")

            # sync_contracts via instruments override
            set_rows_override(
                [
                    _inst(100, symbol="NIFTYX", name="NIFTY", expiry=date(2026, 10, 27)),
                    _inst(
                        101,
                        symbol="EQ",
                        name="INFOSYS",
                        itype="EQ",
                        expiry=None,
                    ),
                ]
            )
            try:
                # EQ with no expiry skipped; only NFO with expiry counted.
                # Need exchange NFO — _inst sets NFO. EQ instrument still NFO in helper;
                # use a real NSE row via manual Instrument.
                nse = Instrument(
                    instrument_token=999,
                    exchange_token=1,
                    tradingsymbol="INFY",
                    name="INFY",
                    last_price=0,
                    expiry=None,
                    strike=0,
                    tick_size=0.05,
                    lot_size=1,
                    instrument_type="EQ",
                    segment="NSE",
                    exchange="NSE",
                )
                set_rows_override(
                    [
                        _inst(100, symbol="NIFTYX", name="NIFTY", expiry=date(2026, 10, 27)),
                        nse,
                    ]
                )
                n = universe.sync_contracts(con, seen_on=today)
                _assert(n == 1, f"sync_contracts count={n}")
            finally:
                clear_rows_override()
        finally:
            con.close()


def test_queue_order_and_chunks() -> None:
    today = date(2026, 10, 25)
    now = datetime(2026, 10, 25, 18, 0, 0, tzinfo=__import__("kitetools.auth", fromlist=["IST"]).IST)

    series = [
        universe.Series(
            "NIFTY",
            date(2026, 10, 27),
            (
                catalog.ContractRow(1, "NIFTY", date(2026, 10, 27), "A", "FUT", 0, 75),
                catalog.ContractRow(2, "NIFTY", date(2026, 10, 27), "B", "CE", 1, 75),
            ),
        ),
        universe.Series(
            "RELIANCE",
            date(2026, 10, 27),
            (catalog.ContractRow(3, "RELIANCE", date(2026, 10, 27), "C", "FUT", 0, 250),),
        ),
        universe.Series(
            "ZZZ",
            date(2026, 10, 27),
            (catalog.ContractRow(4, "ZZZ", date(2026, 10, 27), "D", "FUT", 0, 1),),
        ),
        universe.Series(
            "BANKNIFTY",
            date(2026, 11, 3),
            (catalog.ContractRow(5, "BANKNIFTY", date(2026, 11, 3), "E", "FUT", 0, 15),),
        ),
    ]
    ranks = {
        "NIFTY": catalog.UnderlyingRank(today, "NIFTY", 1, 100, 10.0, 1000.0, "fut_day"),
        "RELIANCE": catalog.UnderlyingRank(today, "RELIANCE", 3, 50, 10.0, 500.0, "fut_day"),
        # ZZZ unranked
    }
    # Contract 1 done → 0 requests; others pending full window → 1 chunk each (60d).
    fetch_states = {
        1: catalog.FetchState(
            token=1,
            interval="minute",
            fetched_to=datetime(2026, 10, 25, 15, 40, 0),
            status="done",
            rows_total=10,
            attempts=1,
            last_error=None,
            updated_at=None,
        ),
    }
    items = planner.build_queue(series, ranks, fetch_states, now)
    _assert(items[0].expiry == date(2026, 10, 27), "earliest expiry first")
    _assert(items[0].underlying == "NIFTY", "highest rank first within expiry")
    _assert(items[1].underlying == "RELIANCE", "second ranked")
    _assert(items[2].underlying == "ZZZ", "unranked last within expiry")
    _assert(items[3].expiry == date(2026, 11, 3), "later expiry after")
    # NIFTY: 1 done + 1 pending = 1 request
    _assert(items[0].pending_requests == 1, f"nifty pending={items[0].pending_requests}")
    _assert(items[1].pending_requests == 1, "reliance 1 req")
    _assert(items[2].pending_requests == 1, "zzz 1 req")

    # Chunk counts — 60 inclusive days ⇒ 1 chunk; 61 inclusive ⇒ 2 chunks
    start = planner.default_backfill_start(now)
    end = planner.fetch_end(now)
    _assert(planner.chunk_count(start, end) == 1, "60d window = 1 chunk")
    _assert(planner.chunk_count(start, start) == 0, "empty range")
    wide_start = now - timedelta(days=60)  # 61 inclusive days vs end.date
    _assert(planner.chunk_count(wide_start, end) == 2, "61 inclusive days = 2 chunks")

    # Partial rollback
    partial = catalog.FetchState(
        token=9,
        interval="minute",
        fetched_to=datetime(2026, 10, 24, 12, 30, 0),
        status="partial",
        rows_total=1,
        attempts=1,
        last_error=None,
        updated_at=None,
    )
    rolled = planner.contract_range_start(partial, now=now)
    _assert(rolled.hour == 9 and rolled.minute == 15, f"partial rollback={rolled}")
    _assert(rolled.date() == date(2026, 10, 24), "partial same day")


def main() -> None:
    tests = [
        test_paths,
        test_archive_idempotent,
        test_catalog_schema,
        test_limiter_spacing,
        test_retry_ladder_and_caps,
        test_rate_limit_min_wait,
        test_input_exception_not_retried,
        test_token_exception_session_expired,
        test_universe_nearest3_and_unfinished,
        test_queue_order_and_chunks,
    ]
    failed = 0
    for fn in tests:
        name = fn.__name__
        try:
            fn()
            print(f"ok  {name}")
        except Exception as exc:
            failed += 1
            print(f"FAIL {name}: {exc}", file=sys.stderr)
    if failed:
        raise SystemExit(f"{failed} test(s) failed")
    print(f"All {len(tests)} tests passed.")


if __name__ == "__main__":
    main()
