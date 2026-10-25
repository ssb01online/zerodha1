"""Offline self-tests for ohlcv Phases 1–2. No Kite login required.

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
from kitetools import auth

from . import archive as archive_mod
from . import catalog
from . import planner
from . import rank as rank_mod
from . import read as read_mod
from . import snapshot
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


def _quote_payload(
    token: int,
    symbol: str,
    *,
    last_trade_time: str,
    volume: int,
    last_price: float,
    ohlc_close: float,
    average_price: float | None = None,
    open_: float = 100.0,
    high: float = 110.0,
    low: float = 90.0,
) -> dict[str, Any]:
    return {
        "instrument_token": token,
        "last_trade_time": last_trade_time,
        "last_price": last_price,
        "volume": volume,
        "average_price": average_price if average_price is not None else last_price,
        "oi": 10.0,
        "oi_day_high": 12.0,
        "oi_day_low": 8.0,
        "buy_quantity": 1,
        "sell_quantity": 2,
        "ohlc": {"open": open_, "high": high, "low": low, "close": ohlc_close},
    }


def test_catalog_v1_to_v2_migration() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "c.duckdb"
        con = catalog.connect(db)
        try:
            # Simulate a v1 catalog (schema_version=1, no daily_snapshots yet).
            con.execute(
                "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            con.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', '1')"
            )
            con.execute(
                """
                CREATE TABLE contracts (
                    token BIGINT PRIMARY KEY,
                    underlying TEXT NOT NULL,
                    expiry DATE NOT NULL,
                    tradingsymbol TEXT NOT NULL,
                    instrument_type TEXT NOT NULL,
                    strike DOUBLE NOT NULL,
                    lot_size INTEGER NOT NULL,
                    first_seen DATE,
                    last_seen DATE
                )
                """
            )
            con.execute(
                """
                CREATE TABLE fetch_state (
                    token BIGINT NOT NULL,
                    interval TEXT NOT NULL,
                    fetched_to TIMESTAMP,
                    status TEXT NOT NULL,
                    rows_total BIGINT NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    updated_at TIMESTAMP,
                    PRIMARY KEY (token, interval)
                )
                """
            )
            con.execute(
                """
                CREATE TABLE underlying_rank (
                    trade_date DATE NOT NULL,
                    underlying TEXT NOT NULL,
                    fut_token BIGINT NOT NULL,
                    volume BIGINT NOT NULL,
                    close DOUBLE NOT NULL,
                    traded_value DOUBLE NOT NULL,
                    source TEXT NOT NULL,
                    PRIMARY KEY (trade_date, underlying)
                )
                """
            )
            con.execute(
                """
                CREATE TABLE runs (
                    run_id BIGINT PRIMARY KEY,
                    command TEXT NOT NULL,
                    started_at TIMESTAMP NOT NULL,
                    finished_at TIMESTAMP,
                    requests INTEGER NOT NULL DEFAULT 0,
                    retries INTEGER NOT NULL DEFAULT 0,
                    errors INTEGER NOT NULL DEFAULT 0,
                    note TEXT
                )
                """
            )
        finally:
            con.close()

        con = catalog.connect(db)
        try:
            catalog.ensure_schema(con)
            _assert(catalog.schema_version(con) == 2, "migrated to v2")
            # daily_snapshots must exist
            con.execute("SELECT COUNT(*) FROM daily_snapshots").fetchone()
            meta = catalog.DailySnapshotMeta(
                trade_date=date(2026, 10, 24),
                status="final",
                taken_at=datetime(2026, 10, 24, 16, 0, 0),
                contracts=10,
                traded_contracts=3,
                missing=0,
                total_volume=100,
                path="x.parquet",
            )
            catalog.record_snapshot(con, meta)
            catalog.record_snapshot(
                con,
                catalog.DailySnapshotMeta(
                    trade_date=date(2026, 10, 23),
                    status="final",
                    taken_at=datetime(2026, 10, 23, 16, 0, 0),
                    contracts=9,
                    traded_contracts=2,
                    missing=0,
                    total_volume=50,
                    path="y.parquet",
                ),
            )
            latest = catalog.latest_snapshot(con, status="final")
            _assert(latest is not None and latest.trade_date == date(2026, 10, 24), "latest")
            prev = catalog.previous_session(con, date(2026, 10, 24))
            _assert(prev == date(2026, 10, 23), f"previous_session={prev}")
        finally:
            con.close()


def test_snapshot_status_boundary() -> None:
    day = date(2026, 10, 27)
    before = datetime.combine(day, datetime.strptime("15:39", "%H:%M").time(), tzinfo=auth.IST)
    at = datetime.combine(day, datetime.strptime("15:40", "%H:%M").time(), tzinfo=auth.IST)
    after = datetime.combine(day, datetime.strptime("16:00", "%H:%M").time(), tzinfo=auth.IST)
    _assert(snapshot.snapshot_status(before) == "intraday", "before 15:40")
    _assert(snapshot.snapshot_status(at) == "final", "at 15:40")
    _assert(snapshot.snapshot_status(after) == "final", "after 15:40")


def test_snapshot_happy_path_and_fields() -> None:
    trade_day = date(2026, 10, 27)
    ltt = "2026-10-27T15:29:00+05:30"
    contracts = [
        catalog.ContractRow(1, "NIFTY", trade_day, "NIFTYFUT", "FUT", 0.0, 75),
        catalog.ContractRow(2, "NIFTY", trade_day, "NIFTYCE", "CE", 25000.0, 75),
        catalog.ContractRow(3, "RELIANCE", trade_day, "RELFUT", "FUT", 0.0, 250),
    ]

    def handler(instruments: list[str]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key in instruments:
            sym = key.split(":", 1)[1]
            if sym == "NIFTYFUT":
                out[key] = _quote_payload(
                    1, sym, last_trade_time=ltt, volume=100, last_price=250.5, ohlc_close=240.0, average_price=245.0
                )
            elif sym == "NIFTYCE":
                out[key] = _quote_payload(
                    2, sym, last_trade_time=ltt, volume=0, last_price=1.0, ohlc_close=2.0, average_price=0.0
                )
            elif sym == "RELFUT":
                out[key] = _quote_payload(
                    3, sym, last_trade_time=ltt, volume=50, last_price=1400.0, ohlc_close=1390.0, average_price=1395.0
                )
        return out

    kite = FakeKite(handler=handler)
    client = KiteClient(kite, quote_interval=0.0, clock=FakeClock(), rng=random.Random(0))  # type: ignore[arg-type]

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        daily_dir = root / "daily"
        staging = root / "staging"
        now = datetime(2026, 10, 27, 16, 0, 0, tzinfo=auth.IST)
        result = snapshot.take_snapshot(
            client,
            contracts,
            now=now,
            run_date=trade_day,
            dest_dir=daily_dir,
            staging_dir=staging,
        )
        _assert(result.status == "final", f"status={result.status}")
        _assert(result.trade_date == trade_day, "trade_date")
        _assert(result.contracts == 3, f"contracts={result.contracts}")
        _assert(result.traded_contracts == 2, f"traded={result.traded_contracts}")
        _assert(result.missing == 0, "missing")
        _assert(result.path.is_file(), "parquet exists")

        rows = read_mod.daily(trade_day, dest_dir=daily_dir)
        by_token = {int(r["token"]): r for r in rows}
        fut = by_token[1]
        _assert(float(fut["close"]) == 250.5, "close=last_price")
        _assert(float(fut["prev_close"]) == 240.0, "prev_close=ohlc.close")
        _assert(float(fut["traded_value"]) == 100 * 245.0, "traded_value")
        ce = by_token[2]
        _assert(ce["open"] is None and ce["high"] is None and ce["low"] is None, "null OHLC when vol=0")
        _assert(int(ce["volume"]) == 0 and ce["traded"] is False, "untraded flag")

        untraded = snapshot.untraded_tokens(trade_day, dest_dir=daily_dir)
        _assert(untraded == {2}, f"untraded={untraded}")

        # Atomic overwrite
        result2 = snapshot.take_snapshot(
            client,
            contracts,
            now=now,
            run_date=trade_day,
            dest_dir=daily_dir,
            staging_dir=staging,
        )
        _assert(result2.path == result.path, "same path overwrite")
        _assert(result2.path.is_file(), "still exists")


def test_snapshot_rejects_weekend_and_batch_failure() -> None:
    trade_day = date(2026, 10, 24)  # Friday session stamped into quotes
    run_day = date(2026, 10, 25)  # Sunday
    contracts = [
        catalog.ContractRow(1, "NIFTY", trade_day, "NIFTYFUT", "FUT", 0.0, 75),
    ]
    ltt = "2026-10-24T15:29:00+05:30"

    def weekend_handler(instruments: list[str]) -> dict[str, Any]:
        return {
            f"NFO:NIFTYFUT": _quote_payload(
                1, "NIFTYFUT", last_trade_time=ltt, volume=0, last_price=250.0, ohlc_close=249.0
            )
        }

    kite = FakeKite(handler=weekend_handler)
    client = KiteClient(kite, quote_interval=0.0, clock=FakeClock(), rng=random.Random(0))  # type: ignore[arg-type]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        try:
            snapshot.take_snapshot(
                client,
                contracts,
                now=datetime(2026, 10, 25, 12, 0, 0, tzinfo=auth.IST),
                run_date=run_day,
                dest_dir=root / "daily",
                staging_dir=root / "staging",
            )
            raise Failure("expected SnapshotRejected on weekend")
        except snapshot.SnapshotRejected as exc:
            _assert("no session today" in str(exc), f"msg={exc}")
        _assert(not (root / "daily" / "20261024.parquet").exists(), "no file on reject")
        _assert(not (root / "daily" / "20261025.parquet").exists(), "no sunday file")

    # Batch failure: no file written
    boom_calls = {"n": 0}

    def boom(instruments: list[str]) -> dict[str, Any]:
        boom_calls["n"] += 1
        raise NetworkException("down")

    kite2 = FakeKite(handler=boom)
    client2 = KiteClient(
        kite2,  # type: ignore[arg-type]
        quote_interval=0.0,
        clock=FakeClock(),
        rng=random.Random(0),
    )
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        try:
            snapshot.take_snapshot(
                client2,
                contracts,
                now=datetime(2026, 10, 24, 16, 0, 0, tzinfo=auth.IST),
                run_date=trade_day,
                dest_dir=root / "daily",
                staging_dir=root / "staging",
            )
            raise Failure("expected RetryExhausted")
        except RetryExhausted:
            pass
        files = list((root / "daily").glob("*.parquet")) if (root / "daily").exists() else []
        _assert(files == [], f"no partial file: {files}")


def test_snapshot_missing_keys_and_rank() -> None:
    trade_day = date(2026, 10, 27)
    ltt = "2026-10-27T15:29:00+05:30"
    contracts = [
        catalog.ContractRow(1, "NIFTY", trade_day, "NIFTYFUT", "FUT", 0.0, 75),
        catalog.ContractRow(2, "NIFTY", trade_day, "NIFTYCE", "CE", 25000.0, 75),
        catalog.ContractRow(3, "BANKNIFTY", trade_day, "BNFUT", "FUT", 0.0, 15),
        catalog.ContractRow(4, "BANKNIFTY", trade_day, "BNCE", "CE", 50000.0, 15),
    ]

    def handler(instruments: list[str]) -> dict[str, Any]:
        # Omit BNCE intentionally → missing=1
        out: dict[str, Any] = {}
        mapping = {
            "NIFTYFUT": _quote_payload(
                1, "NIFTYFUT", last_trade_time=ltt, volume=10, last_price=100.0, ohlc_close=99.0, average_price=100.0
            ),
            "NIFTYCE": _quote_payload(
                2, "NIFTYCE", last_trade_time=ltt, volume=20, last_price=5.0, ohlc_close=4.0, average_price=5.0
            ),
            "BNFUT": _quote_payload(
                3, "BNFUT", last_trade_time=ltt, volume=5, last_price=200.0, ohlc_close=190.0, average_price=200.0
            ),
        }
        for key in instruments:
            sym = key.split(":", 1)[1]
            if sym in mapping:
                out[key] = mapping[sym]
        return out

    kite = FakeKite(handler=handler)
    client = KiteClient(kite, quote_interval=0.0, clock=FakeClock(), rng=random.Random(0))  # type: ignore[arg-type]

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        daily_dir = root / "daily"
        result = snapshot.take_snapshot(
            client,
            contracts,
            now=datetime(2026, 10, 27, 16, 5, 0, tzinfo=auth.IST),
            run_date=trade_day,
            dest_dir=daily_dir,
            staging_dir=root / "staging",
        )
        _assert(result.missing == 1, f"missing={result.missing}")
        _assert(result.contracts == 3, f"rows={result.contracts}")

        db = root / "c.duckdb"
        con = catalog.connect(db)
        try:
            catalog.ensure_schema(con)
            # Seed contracts so nearest FUT lookup works for fut_token/close.
            catalog.upsert_contracts(con, contracts, seen_on=trade_day)
            rr = rank_mod.rank_from_snapshot(con, trade_day, dest_dir=daily_dir)
            _assert(rr.source == "quote", f"source={rr.source}")
            _assert(rr.upserted == 2, f"upserted={rr.upserted}")
            ranks = rank_mod.load_ranks(con)
            # NIFTY traded_value = 10*100 + 20*5 = 1100; BANKNIFTY = 5*200 = 1000
            _assert(abs(ranks["NIFTY"].traded_value - 1100.0) < 1e-6, f"nifty tv={ranks['NIFTY'].traded_value}")
            _assert(abs(ranks["BANKNIFTY"].traded_value - 1000.0) < 1e-6, "bn tv")
            _assert(ranks["NIFTY"].fut_token == 1, "nifty fut_token")
            _assert(ranks["NIFTY"].source == "quote", "rank source")

            # refresh_rank prefers final snapshot
            catalog.record_snapshot(
                con,
                catalog.DailySnapshotMeta(
                    trade_date=trade_day,
                    status="final",
                    taken_at=datetime(2026, 10, 27, 16, 5, 0),
                    contracts=result.contracts,
                    traded_contracts=result.traded_contracts,
                    missing=result.missing,
                    total_volume=result.total_volume,
                    path=str(result.path),
                ),
            )
            # Clear ranks and force refresh via snapshot (no client needed).
            con.execute("DELETE FROM underlying_rank")
            rr2 = rank_mod.refresh_rank(
                None, con, today=trade_day, force=True, dest_dir=daily_dir
            )
            _assert(rr2.source == "quote", f"refresh source={rr2.source}")
            _assert(rr2.upserted == 2, f"refresh upserted={rr2.upserted} errors={rr2.errors}")

            # series_daily / daily readers
            series_rows = read_mod.series_daily("NIFTY", trade_day, dest_dir=daily_dir)
            _assert(len(series_rows) == 2, f"series_daily={len(series_rows)}")
        finally:
            con.close()


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
        test_catalog_v1_to_v2_migration,
        test_snapshot_status_boundary,
        test_snapshot_happy_path_and_fields,
        test_snapshot_rejects_weekend_and_batch_failure,
        test_snapshot_missing_keys_and_rank,
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
