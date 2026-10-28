"""Offline self-tests for ohlcv Phases 1–5. No Kite login required.

Run: uv run python -m ohlcv.selftest
"""

from __future__ import annotations

import json
import os
import random
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from unittest import mock

import duckdb

from instruments.cache import clear_rows_override, set_rows_override
from instruments.models import Instrument
from kiteconnect.exceptions import InputException, NetworkException, TokenException
from kitetools import auth

from . import archive as archive_mod
from . import catalog
from . import continuous as continuous_mod
from . import fetch as fetch_mod
from . import planner
from . import rank as rank_mod
from . import read as read_mod
from . import snapshot
from . import status as status_mod
from . import universe
from .client import (
    KiteClient,
    RequestRejected,
    RetryExhausted,
    SessionExpired,
)
from . import markets as markets_mod
from . import planner as planner_mod
from . import universe as universe_mod
from .paths import (
    CATALOG_PATH,
    OHLCV_DIR,
    paths_for,
    sanitize_underlying,
    series_path,
    ymd,
)
from .progress import InterruptController, setup_run_logging


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
    exchange: str = "NFO",
) -> Instrument:
    exch = exchange.strip().upper()
    if exch == "NFO":
        segment = f"NFO-{itype}"
    elif exch in ("MCX", "CDS") and itype in ("FUT", "CE", "PE"):
        segment = f"{exch}-{'FUT' if itype == 'FUT' else 'OPT'}"
    else:
        segment = exch
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
        segment=segment,
        exchange=exch,
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


def test_markets_and_paths_for() -> None:
    nfo = markets_mod.get_market("nfo")
    mcx = markets_mod.get_market("MCX")
    cds = markets_mod.get_market_by_exchange("cds")
    _assert(nfo.exchange == "NFO", "nfo exchange")
    _assert(mcx.id == "mcx" and mcx.session_start.hour == 9 and mcx.session_end.hour == 23, "mcx session")
    _assert(cds.id == "cds" and cds.session_end.hour == 17, "cds session")
    _assert(nfo.continuous_day and mcx.continuous_day and cds.continuous_day, "continuous")
    _assert(nfo.sync_instrument_types is None, "nfo syncs all expiry types")
    _assert(
        mcx.sync_instrument_types == markets_mod.DERIVATIVE_TYPES,
        "mcx FUT+CE+PE sync",
    )
    _assert(
        cds.sync_instrument_types == markets_mod.DERIVATIVE_TYPES,
        "cds FUT+CE+PE sync",
    )
    raised = False
    try:
        markets_mod.get_market("bfo")
    except ValueError:
        raised = True
    _assert(raised, "unknown market_id raises")

    nfo_paths = paths_for("nfo")
    mcx_paths = paths_for("mcx")
    cds_paths = paths_for("cds")
    _assert(nfo_paths.root.name == "nfo", f"nfo root={nfo_paths.root}")
    _assert(mcx_paths.root.name == "mcx" and mcx_paths.catalog_path.name == "catalog.duckdb", "mcx paths")
    _assert(cds_paths.probe_dir.name == "_probe" and "cds" in cds_paths.probe_dir.parts, "cds probe")
    _assert(mcx_paths.continuous_dir.name == "continuous", "mcx continuous_dir")
    _assert(OHLCV_DIR == nfo_paths.root, "OHLCV_DIR is nfo alias")
    _assert(CATALOG_PATH == nfo_paths.catalog_path, "CATALOG_PATH is nfo alias")
    _assert(universe_mod.SESSION_END == nfo.session_end, "universe SESSION_END from NFO")
    _assert(planner_mod.SESSION_START == nfo.session_start, "planner SESSION_START from NFO")
    _assert(planner_mod.SESSION_END == nfo.session_end, "planner SESSION_END from NFO")


def test_planner_fetch_end_per_market() -> None:
    """Session bounds come from MarketProfile (NFO default unchanged)."""
    after_nfo = datetime(2026, 10, 27, 18, 0, 0, tzinfo=auth.IST)
    during_mcx = datetime(2026, 10, 27, 20, 0, 0, tzinfo=auth.IST)
    after_cds = datetime(2026, 10, 27, 18, 0, 0, tzinfo=auth.IST)

    nfo_day = planner_mod.day_fetch_end(after_nfo)
    _assert(nfo_day.hour == 15 and nfo_day.minute == 40, f"nfo day end={nfo_day}")
    mcx_day = planner_mod.day_fetch_end(during_mcx, market="mcx")
    _assert(
        mcx_day.date() == date(2026, 10, 26) and mcx_day.hour == 23 and mcx_day.minute == 30,
        f"mcx before close excludes today: {mcx_day}",
    )
    mcx_after = planner_mod.day_fetch_end(
        datetime(2026, 10, 27, 23, 45, 0, tzinfo=auth.IST), market="mcx"
    )
    _assert(mcx_after.hour == 23 and mcx_after.minute == 30, f"mcx after close={mcx_after}")
    cds_day = planner_mod.day_fetch_end(after_cds, market="cds")
    _assert(cds_day.hour == 17 and cds_day.minute == 0, f"cds day end={cds_day}")

    mcx_min = planner_mod.fetch_end(during_mcx, market="mcx")
    _assert(
        mcx_min.hour == 19 and mcx_min.minute == 59,
        f"mcx minute during session floors prior minute: {mcx_min}",
    )
    rolled = planner_mod.partial_rollback(
        datetime(2026, 10, 27, 18, 30, 0, tzinfo=auth.IST), market="mcx"
    )
    _assert(rolled.hour == 9 and rolled.minute == 0, f"mcx partial rollback={rolled}")
    nfo_rolled = planner_mod.partial_rollback(
        datetime(2026, 10, 27, 12, 30, 0, tzinfo=auth.IST)
    )
    _assert(nfo_rolled.hour == 9 and nfo_rolled.minute == 15, f"nfo partial={nfo_rolled}")


def test_fetch_chunk_windows_mcx_session() -> None:
    start = datetime(2026, 10, 20, 9, 0, 0, tzinfo=auth.IST)
    end = datetime(2026, 10, 22, 23, 30, 0, tzinfo=auth.IST)
    windows = fetch_mod._chunk_windows(start, end, chunk_days=60, market="mcx")
    _assert(len(windows) == 1, f"windows={len(windows)}")
    _assert(windows[0][0] == start, "start preserved")
    _assert(windows[0][1] == end, f"end={windows[0][1]}")
    # Intermediate chunk boundary uses MCX session_end / session_start.
    wide_end = datetime(2026, 12, 20, 23, 30, 0, tzinfo=auth.IST)
    multi = fetch_mod._chunk_windows(start, wide_end, chunk_days=60, market="mcx")
    _assert(len(multi) >= 2, f"multi chunks={len(multi)}")
    mid_end = multi[0][1]
    _assert(mid_end.hour == 23 and mid_end.minute == 30, f"mcx mid chunk end={mid_end}")
    _assert(multi[1][0].hour == 9 and multi[1][0].minute == 0, f"mcx next start={multi[1][0]}")


def test_fetch_mcx_day_uses_market_session() -> None:
    """MCX day fetch watermarks to 23:30 and writes under the caller day_dir."""
    now = datetime(2026, 10, 27, 23, 45, 0, tzinfo=auth.IST)
    expiry = date(2026, 12, 4)

    def handler(token: int, from_date: Any, to_date: Any, interval: str, **kwargs: Any) -> list:
        _assert(interval == "day", f"interval={interval}")
        return _synth_day_candles(token, from_date, to_date, n=3, price=float(token))

    client, kite, _clock = _make_client(handler)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db = root / "c.duckdb"
        day_dir = root / "day"
        staging = root / "staging"
        con = catalog.connect(db)
        try:
            catalog.ensure_schema(con)
            series = _seed_series_contracts(
                con, underlying="GOLD", expiry=expiry, tokens=[1001], seen_on=now.date()
            )
            result = fetch_mod.process_series(
                client,
                con,
                series,
                {},
                now=now,
                interval="day",
                series_root=day_dir,
                staging_dir=staging,
                market="mcx",
            )
            _assert(result.fetched == 1, f"fetched={result.fetched}")
            _assert(result.path is not None and result.path.is_file(), "parquet")
            st = catalog.get_fetch_state(con, 1001, "day")
            _assert(st is not None and st.status == "done", f"status={st}")
            _assert(st.fetched_to is not None, "watermark set")
            _assert(
                st.fetched_to.hour == 23 and st.fetched_to.minute == 30,
                f"mcx wm={st.fetched_to}",
            )
            end = fetch_mod.contract_end(now, expiry, interval="day", market="mcx")
            _assert(end.hour == 23 and end.minute == 30, f"contract_end={end}")
            _assert(kite.calls == 1, f"calls={kite.calls}")
        finally:
            con.close()


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


def test_sync_mcx_cds_options_and_types_override() -> None:
    """MCX/CDS default sync upserts FUT+CE+PE; --types fut override syncs FUT only."""
    today = date(2026, 10, 28)
    set_rows_override(
        [
            _inst(1, symbol="GOLD26DECFUT", name="GOLD", expiry=date(2026, 12, 4), exchange="MCX"),
            _inst(
                2,
                symbol="GOLD26OCTCE",
                name="GOLD",
                itype="CE",
                expiry=date(2026, 10, 30),
                strike=144000,
                exchange="MCX",
            ),
            _inst(3, symbol="USDINR26NOVFUT", name="USDINR", expiry=date(2026, 11, 26), exchange="CDS"),
            _inst(
                4,
                symbol="USDINR26OCTCE",
                name="USDINR",
                itype="CE",
                expiry=date(2026, 10, 28),
                strike=88.0,
                exchange="CDS",
            ),
            _inst(5, symbol="NIFTY26NOVFUT", name="NIFTY", expiry=date(2026, 11, 24), exchange="NFO"),
            _inst(
                6,
                symbol="NIFTY26NOVCE",
                name="NIFTY",
                itype="CE",
                expiry=date(2026, 11, 24),
                strike=24000,
                exchange="NFO",
            ),
        ]
    )
    try:
        with tempfile.TemporaryDirectory() as tmp:
            mcx_db = Path(tmp) / "mcx.duckdb"
            cds_db = Path(tmp) / "cds.duckdb"
            nfo_db = Path(tmp) / "nfo.duckdb"
            for label, db, market, expect_tokens in (
                ("mcx", mcx_db, "mcx", {1, 2}),
                ("cds", cds_db, "cds", {3, 4}),
                ("nfo", nfo_db, "nfo", {5, 6}),
            ):
                con = catalog.connect(db)
                try:
                    catalog.ensure_schema(con)
                    n = universe.sync_contracts(con, market=market, seen_on=today)
                    rows = catalog.list_contracts(con)
                    tokens = {r.token for r in rows}
                    _assert(n == len(expect_tokens), f"{label} sync count={n}")
                    _assert(tokens == expect_tokens, f"{label} tokens={tokens}")
                    series = universe.select_series(con, today=today, market=market)
                    keys = {(s.underlying, s.expiry) for s in series}
                    if label == "mcx":
                        _assert(("GOLD", date(2026, 12, 4)) in keys, "mcx FUT series")
                        _assert(("GOLD", date(2026, 10, 30)) in keys, "mcx CE series")
                    if label == "cds":
                        _assert(("USDINR", date(2026, 11, 26)) in keys, "cds FUT series")
                        _assert(("USDINR", date(2026, 10, 28)) in keys, "cds CE series")
                finally:
                    con.close()

            # --types fut override: FUT only (does not delete prior CE rows if re-synced empty —
            # here we use a fresh DB).
            fut_db = Path(tmp) / "mcx_fut.duckdb"
            con = catalog.connect(fut_db)
            try:
                catalog.ensure_schema(con)
                n = universe.sync_contracts(
                    con,
                    market="mcx",
                    seen_on=today,
                    allowed_types=markets_mod.FUT_TYPES,
                )
                tokens = {r.token for r in catalog.list_contracts(con)}
                _assert(n == 1 and tokens == {1}, f"mcx --types fut tokens={tokens}")
            finally:
                con.close()
    finally:
        clear_rows_override()


def test_dual_nearest3_fut_and_opt_pools() -> None:
    """Weekly CE expiries must not crowd a farther monthly FUT out of the live set."""
    today = date(2026, 10, 28)
    fut_exp = date(2026, 12, 27)  # ~T+60
    ce_exps = [
        date(2026, 11, 4),
        date(2026, 11, 11),
        date(2026, 11, 18),
        date(2026, 11, 25),  # 4th weekly — should be excluded
    ]
    rows = [
        catalog.ContractRow(1, "GOLD", fut_exp, "GOLD26DECFUT", "FUT", 0.0, 1),
    ]
    tok = 10
    for exp in ce_exps:
        rows.append(
            catalog.ContractRow(tok, "GOLD", exp, f"GOLDCE{tok}", "CE", 144000.0, 1)
        )
        tok += 1
        rows.append(
            catalog.ContractRow(tok, "GOLD", exp, f"GOLDPE{tok}", "PE", 144000.0, 1)
        )
        tok += 1
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "c.duckdb"
        con = catalog.connect(db)
        try:
            catalog.ensure_schema(con)
            catalog.upsert_contracts(con, rows, seen_on=today)
            series = universe.select_series(con, today=today, market="mcx")
            keys = {(s.underlying, s.expiry) for s in series}
            _assert(("GOLD", fut_exp) in keys, "monthly FUT kept despite weekly CEs")
            for exp in ce_exps[:3]:
                _assert(("GOLD", exp) in keys, f"nearest CE expiry {exp}")
            _assert(("GOLD", ce_exps[3]) not in keys, "4th weekly CE excluded")
            _assert(len(keys) == 4, f"expected 1 FUT + 3 CE series, got {keys}")

            opt_only = universe.select_series(
                con, today=today, market="mcx", types="opt"
            )
            opt_keys = {(s.underlying, s.expiry) for s in opt_only}
            _assert(fut_exp not in {e for _, e in opt_keys}, "opt filter drops FUT expiry")
            _assert(all(c.instrument_type in ("CE", "PE") for s in opt_only for c in s.contracts), "opt types")
        finally:
            con.close()


def test_build_queue_types_filter_excludes_fut() -> None:
    today = date(2026, 10, 25)
    now = datetime(2026, 10, 25, 18, 0, 0, tzinfo=auth.IST)
    series = [
        universe.Series(
            "GOLD",
            date(2026, 10, 30),
            (
                catalog.ContractRow(1, "GOLD", date(2026, 10, 30), "GFUT", "FUT", 0.0, 1),
                catalog.ContractRow(2, "GOLD", date(2026, 10, 30), "GCE", "CE", 144000.0, 1),
                catalog.ContractRow(3, "GOLD", date(2026, 10, 30), "GPE", "PE", 144000.0, 1),
            ),
        ),
    ]
    items_all = planner.build_queue(series, {}, {}, now, types="all")
    _assert(len(items_all) == 1 and items_all[0].contracts == 3, "all keeps 3")
    items_opt = planner.build_queue(series, {}, {}, now, types="opt")
    _assert(len(items_opt) == 1 and items_opt[0].contracts == 2, "opt drops FUT")
    items_fut = planner.build_queue(series, {}, {}, now, types="fut")
    _assert(len(items_fut) == 1 and items_fut[0].contracts == 1, "fut keeps FUT only")
    filtered = universe.filter_series_by_types(series, "opt")
    _assert(
        all(c.instrument_type != "FUT" for c in filtered[0].contracts),
        "filter_series_by_types drops FUT",
    )


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

    # Day backfill: 2000 inclusive days ⇒ 1 chunk; day_fetch_end after close = today 15:40
    day_start = planner.default_day_backfill_start(now)
    day_end = planner.day_fetch_end(now)
    _assert(
        (day_end.date() - day_start.date()).days + 1 == planner.DAY_BACKFILL_DAYS,
        "day backfill inclusive span",
    )
    _assert(
        planner.chunk_count(day_start, day_end, chunk_days=planner.DAY_CHUNK_DAYS) == 1,
        "2000d day window = 1 chunk",
    )
    _assert(day_end.date() == now.date() and day_end.hour == 15, "day end after close = today")
    before_close = datetime(2026, 10, 25, 12, 0, 0, tzinfo=auth.IST)
    _assert(
        planner.day_fetch_end(before_close).date() == date(2026, 10, 24),
        "day end before close excludes today",
    )
    day_items = planner.build_queue(series, ranks, {}, now, interval="day")
    _assert(day_items[0].pending_requests == 2, f"day nifty pending={day_items[0].pending_requests}")
    _assert(day_items[1].pending_requests == 1, "day reliance 1 req")

    # --days parsing and wider minute windows
    _assert(planner.parse_days_arg(None) == 60, "default days")
    _assert(planner.parse_days_arg("60") == 60, "str 60")
    _assert(planner.parse_days_arg(120) == 120, "int 120")
    _assert(planner.parse_days_arg("all") is None, "all")
    _assert(planner.parse_days_arg("ALL") is None, "ALL")
    try:
        planner.parse_days_arg(0)
        _assert(False, "expected ValueError for 0")
    except ValueError:
        pass
    try:
        planner.parse_days_arg("nope")
        _assert(False, "expected ValueError for nope")
    except ValueError:
        pass
    start_120 = planner.default_backfill_start(now, backfill_days=120)
    _assert(
        (end.date() - start_120.date()).days + 1 == 120,
        "120d inclusive span",
    )
    _assert(planner.chunk_count(start_120, end) == 2, "120d = 2 minute chunks")
    start_all = planner.default_backfill_start(now, backfill_days=None)
    _assert(
        (end.date() - start_all.date()).days + 1 == planner.DAY_BACKFILL_DAYS,
        "all estimate uses DAY_BACKFILL_DAYS",
    )
    items_120 = planner.build_queue(
        series, ranks, {}, now, interval="minute", backfill_days=120
    )
    # No states → each contract 2 chunks
    _assert(items_120[0].pending_requests == 4, f"nifty 120d pending={items_120[0].pending_requests}")


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

    mcx_before = datetime.combine(day, datetime.strptime("23:29", "%H:%M").time(), tzinfo=auth.IST)
    mcx_at = datetime.combine(day, datetime.strptime("23:30", "%H:%M").time(), tzinfo=auth.IST)
    _assert(snapshot.snapshot_status(mcx_before, market="mcx") == "intraday", "mcx before 23:30")
    _assert(snapshot.snapshot_status(mcx_at, market="mcx") == "final", "mcx at 23:30")
    cds_before = datetime.combine(day, datetime.strptime("16:59", "%H:%M").time(), tzinfo=auth.IST)
    cds_at = datetime.combine(day, datetime.strptime("17:00", "%H:%M").time(), tzinfo=auth.IST)
    _assert(snapshot.snapshot_status(cds_before, market="cds") == "intraday", "cds before 17:00")
    _assert(snapshot.snapshot_status(cds_at, market="cds") == "final", "cds at 17:00")


def test_snapshot_mcx_quote_prefix_and_rank() -> None:
    """MCX snapshot uses MCX: quote keys and ranks from market daily_dir."""
    trade_day = date(2026, 10, 27)
    ltt = "2026-10-27T23:20:00+05:30"
    contracts = [
        catalog.ContractRow(101, "GOLD", trade_day, "GOLD26DECFUT", "FUT", 0.0, 1),
        catalog.ContractRow(102, "GOLD", trade_day, "GOLDCE", "CE", 144000.0, 1),
    ]
    seen_keys: list[str] = []

    def handler(instruments: list[str]) -> dict[str, Any]:
        seen_keys.extend(instruments)
        out: dict[str, Any] = {}
        for key in instruments:
            _assert(key.startswith("MCX:"), f"expected MCX prefix, got {key}")
            sym = key.split(":", 1)[1]
            if sym == "GOLD26DECFUT":
                out[key] = _quote_payload(
                    101,
                    sym,
                    last_trade_time=ltt,
                    volume=10,
                    last_price=144000.0,
                    ohlc_close=143000.0,
                    average_price=143500.0,
                )
            else:
                out[key] = _quote_payload(
                    102,
                    sym,
                    last_trade_time=ltt,
                    volume=0,
                    last_price=100.0,
                    ohlc_close=90.0,
                    average_price=0.0,
                )
        return out

    kite = FakeKite(handler=handler)
    client = KiteClient(kite, quote_interval=0.0, clock=FakeClock(), rng=random.Random(0))  # type: ignore[arg-type]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        daily_dir = root / "daily"
        staging = root / "staging"
        now = datetime(2026, 10, 27, 23, 45, 0, tzinfo=auth.IST)
        result = snapshot.take_snapshot(
            client,
            contracts,
            market="mcx",
            now=now,
            run_date=trade_day,
            dest_dir=daily_dir,
            staging_dir=staging,
        )
        _assert(result.status == "final", f"status={result.status}")
        _assert(all(k.startswith("MCX:") for k in seen_keys), f"keys={seen_keys}")
        untraded = snapshot.untraded_tokens(trade_day, dest_dir=daily_dir)
        _assert(untraded == {102}, f"untraded={untraded}")

        con = catalog.connect(root / "c.duckdb")
        try:
            catalog.ensure_schema(con)
            rr = rank_mod.rank_from_snapshot(
                con, trade_day, market="mcx", dest_dir=daily_dir
            )
            _assert(rr.upserted == 1 and rr.source == "quote", f"rank={rr}")
            ranks = rank_mod.load_ranks(con)
            _assert("GOLD" in ranks, "GOLD ranked")
            _assert(ranks["GOLD"].traded_value == 10 * 143500.0, "traded_value")
        finally:
            con.close()


def test_continuous_nearest_fut_and_write() -> None:
    today = date(2026, 10, 28)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        con = catalog.connect(root / "c.duckdb")
        try:
            catalog.ensure_schema(con)
            raised = False
            try:
                continuous_mod.nearest_live_fut(con, "GOLD", seen_on=today)
            except Exception:
                raised = True
            _assert(
                continuous_mod.nearest_live_fut(con, "GOLD", seen_on=today) is None,
                "no live FUT",
            )
            _assert(not raised, "nearest returns None not raise")

            catalog.upsert_contracts(
                con,
                [
                    catalog.ContractRow(
                        501, "GOLD", date(2026, 12, 4), "GOLD26DECFUT", "FUT", 0.0, 1
                    ),
                    catalog.ContractRow(
                        502, "GOLD", date(2027, 2, 5), "GOLD27FEBFUT", "FUT", 0.0, 1
                    ),
                    catalog.ContractRow(
                        503, "GOLD", date(2026, 10, 30), "GOLDCE", "CE", 144000.0, 1
                    ),
                ],
                seen_on=today,
            )
            fut = continuous_mod.nearest_live_fut(con, "GOLD", seen_on=today)
            _assert(fut is not None and fut.token == 501, f"nearest={fut}")

            def hist_handler(
                token: int, from_date: Any, to_date: Any, interval: str, **kwargs: Any
            ) -> list:
                _assert(kwargs.get("continuous") is True, "continuous flag")
                _assert(interval == "day", "day interval")
                _assert(token == 501, f"token={token}")
                return [
                    {
                        "date": datetime(2026, 10, 1, tzinfo=auth.IST),
                        "open": 1.0,
                        "high": 2.0,
                        "low": 0.5,
                        "close": 1.5,
                        "volume": 10,
                        "oi": 100,
                    },
                    {
                        "date": datetime(2026, 10, 2, tzinfo=auth.IST),
                        "open": 1.5,
                        "high": 2.5,
                        "low": 1.0,
                        "close": 2.0,
                        "volume": 20,
                        "oi": 110,
                    },
                ]

            client, _, _ = _make_client(hist_handler)
            dest = root / "continuous" / "GOLD.parquet"
            result = continuous_mod.fetch_continuous(
                client,
                con,
                "GOLD",
                market="mcx",
                from_date=date(2026, 10, 1),
                to_date=date(2026, 10, 2),
                seen_on=today,
                dest_path=dest,
            )
            _assert(result.rows == 2 and result.path.is_file(), f"result={result}")
            _assert(result.tradingsymbol == "GOLD26DECFUT", "symbol")

            rejected = False
            try:
                continuous_mod.fetch_continuous(
                    client,
                    con,
                    "SILVER",
                    market="mcx",
                    seen_on=today,
                    dest_path=root / "continuous" / "SILVER.parquet",
                )
            except continuous_mod.ContinuousRejected:
                rejected = True
            _assert(rejected, "missing underlying rejected")
        finally:
            con.close()


def test_can_skip_untraded_uses_market_daily_dir() -> None:
    """fetch.can_skip_untraded reads paths_for(market).daily_dir (not NFO alias)."""
    now = datetime(2026, 10, 23, 18, 0, 0, tzinfo=auth.IST)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # Pretend MCX daily lives under this temp root.
        mcx_daily = root / "ohlcv" / "mcx" / "daily"
        fake_paths = paths_for("mcx")
        # Build a MarketPaths-like stand-in via mock of paths_for.
        from .paths import MarketPaths

        stub = MarketPaths(
            root=root / "ohlcv" / "mcx",
            probe_dir=root / "ohlcv" / "mcx" / "_probe",
            daily_dir=mcx_daily,
            day_dir=root / "ohlcv" / "mcx" / "day",
            minute_dir=root / "ohlcv" / "mcx" / "minute",
            continuous_dir=root / "ohlcv" / "mcx" / "continuous",
            staging_dir=root / "ohlcv" / "mcx" / "_staging",
            logs_dir=root / "ohlcv" / "mcx" / "logs",
            catalog_path=root / "ohlcv" / "mcx" / "catalog.duckdb",
        )
        con = catalog.connect(root / "c.duckdb")
        try:
            catalog.ensure_schema(con)
            expiry = date(2026, 10, 27)
            for day, vol in ((date(2026, 10, 22), 0), (date(2026, 10, 23), 0)):
                rows = [_snap_row(77, "GOLD", expiry, day, volume=vol)]
                _write_final_snapshot(con, day, rows, mcx_daily)

            cache: dict[date, set[int] | None] = {}
            # Watermark at MCX session_end so first pending weekday is Thu 22.
            with mock.patch.object(fetch_mod, "paths_for", return_value=stub):
                ok = fetch_mod.can_skip_untraded(
                    con,
                    77,
                    datetime(2026, 10, 21, 23, 30, 0),
                    datetime(2026, 10, 23, 23, 30, 0),
                    untraded_cache=cache,
                    market="mcx",
                )
            _assert(ok is True, f"skip={ok} cache={cache}")
            _assert(fake_paths.daily_dir.name == "daily", "sanity real paths")
        finally:
            con.close()


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


def _synth_candles(
    token: int,
    from_date: Any,
    to_date: Any,
    *,
    n: int = 3,
    price: float = 100.0,
) -> list[dict[str, Any]]:
    """Synthetic minute bars at 09:15.. of the from_date day."""
    if isinstance(from_date, datetime):
        day = from_date.date() if from_date.tzinfo is None else from_date.astimezone(auth.IST).date()
    elif isinstance(from_date, date):
        day = from_date
    else:
        day = date.fromisoformat(str(from_date)[:10])
    out: list[dict[str, Any]] = []
    for i in range(n):
        ts = datetime(day.year, day.month, day.day, 9, 15 + i, tzinfo=auth.IST)
        out.append(
            {
                "date": ts,
                "open": price,
                "high": price + 1,
                "low": price - 1,
                "close": price + 0.5,
                "volume": 10 + i,
                "oi": 1000 + i,
            }
        )
    return out


def _synth_day_candles(
    token: int,
    from_date: Any,
    to_date: Any,
    *,
    n: int = 5,
    price: float = 100.0,
) -> list[dict[str, Any]]:
    """Synthetic day bars starting at from_date (or last n weekdays ending at to_date)."""
    if isinstance(to_date, datetime):
        end = to_date.date() if to_date.tzinfo is None else to_date.astimezone(auth.IST).date()
    elif isinstance(to_date, date):
        end = to_date
    else:
        end = date.fromisoformat(str(to_date)[:10])
    days: list[date] = []
    d = end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    out: list[dict[str, Any]] = []
    for i, day in enumerate(days):
        ts = datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=auth.IST)
        out.append(
            {
                "date": ts,
                "open": price,
                "high": price + 1,
                "low": price - 1,
                "close": price + 0.5,
                "volume": 100 + i,
                "oi": 2000 + i,
            }
        )
    return out


def _make_client(handler: Callable[..., Any]) -> tuple[KiteClient, FakeKite, FakeClock]:
    kite = FakeKite(handler)
    clock = FakeClock()
    client = KiteClient(kite, hist_interval=0.0, quote_interval=0.0, clock=clock, rng=random.Random(0))
    return client, kite, clock


def _seed_series_contracts(
    con: Any,
    *,
    underlying: str,
    expiry: date,
    tokens: list[int],
    seen_on: date,
) -> universe.Series:
    rows = [
        catalog.ContractRow(
            token=t,
            underlying=underlying,
            expiry=expiry,
            tradingsymbol=f"{underlying}{t}",
            instrument_type="CE" if i else "FUT",
            strike=float(i * 100),
            lot_size=50,
            first_seen=seen_on,
            last_seen=seen_on,
        )
        for i, t in enumerate(tokens)
    ]
    catalog.upsert_contracts(con, rows, seen_on=seen_on)
    return universe.Series(underlying, expiry, tuple(rows))


def _write_final_snapshot(
    con: Any,
    trade_date: date,
    rows: list[dict[str, Any]],
    dest_dir: Path,
) -> None:
    """Write a minimal daily parquet + catalog row for skip-rule tests."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"{ymd(trade_date)}.parquet"
    # Reuse snapshot writer via CSV→parquet path by calling internal helper pieces.
    staging = dest_dir / "_staging"
    snapshot._write_parquet(rows, path, staging_dir=staging)  # noqa: SLF001
    catalog.record_snapshot(
        con,
        catalog.DailySnapshotMeta(
            trade_date=trade_date,
            status="final",
            taken_at=datetime(trade_date.year, trade_date.month, trade_date.day, 16, 0),
            contracts=len(rows),
            traded_contracts=sum(1 for r in rows if r.get("traded")),
            missing=0,
            total_volume=sum(int(r.get("volume") or 0) for r in rows),
            path=str(path),
        ),
    )


def _snap_row(
    token: int,
    underlying: str,
    expiry: date,
    trade_date: date,
    *,
    volume: int,
) -> dict[str, Any]:
    return {
        "trade_date": trade_date.isoformat(),
        "token": token,
        "underlying": underlying,
        "expiry": expiry.isoformat(),
        "tradingsymbol": f"{underlying}{token}",
        "instrument_type": "FUT",
        "strike": 0.0,
        "lot_size": 50,
        "open": 100.0 if volume else None,
        "high": 110.0 if volume else None,
        "low": 90.0 if volume else None,
        "close": 105.0,
        "prev_close": 104.0,
        "volume": volume,
        "average_price": 105.0 if volume else 0.0,
        "traded_value": float(volume) * 105.0,
        "traded": volume > 0,
        "oi": 1.0,
        "oi_day_high": 1.0,
        "oi_day_low": 1.0,
        "buy_quantity": 0,
        "sell_quantity": 0,
        "last_trade_time": f"{trade_date.isoformat()}T15:29:00+05:30",
    }


def test_fetch_day_backfill_and_rerun() -> None:
    """Day interval: one hist call per contract over multi-month span; parquet under day/."""
    now = datetime(2026, 10, 25, 18, 0, 0, tzinfo=auth.IST)
    expiry = date(2026, 10, 27)
    call_spans: list[tuple[Any, Any]] = []

    def handler(token: int, from_date: Any, to_date: Any, interval: str, **kwargs: Any) -> list:
        _assert(interval == "day", f"interval={interval}")
        call_spans.append((from_date, to_date))
        return _synth_day_candles(token, from_date, to_date, n=5, price=float(token))

    client, kite, _clock = _make_client(handler)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db = root / "c.duckdb"
        day_dir = root / "day"
        staging = root / "staging"
        con = catalog.connect(db)
        try:
            catalog.ensure_schema(con)
            series = _seed_series_contracts(
                con, underlying="NIFTY", expiry=expiry, tokens=[20, 21], seen_on=now.date()
            )
            states: dict[int, catalog.FetchState] = {}
            result = fetch_mod.process_series(
                client,
                con,
                series,
                states,
                now=now,
                interval="day",
                series_root=day_dir,
                staging_dir=staging,
            )
            _assert(not result.locked, "not locked")
            _assert(result.fetched == 2, f"fetched={result.fetched}")
            _assert(result.path is not None and result.path.is_file(), "parquet written")
            _assert(kite.calls == 2, f"hist calls={kite.calls}")
            _assert(str(day_dir) in str(result.path), f"path under day/: {result.path}")
            # Full ~2000d window still one request (span >> contract life).
            for fr, to in call_spans:
                fr_d = fr.date() if isinstance(fr, datetime) else date.fromisoformat(str(fr)[:10])
                to_d = to.date() if isinstance(to, datetime) else date.fromisoformat(str(to)[:10])
                _assert((to_d - fr_d).days + 1 >= 100, f"wide span {fr_d}..{to_d}")

            dcon = duckdb.connect()
            try:
                rows = dcon.execute(
                    "SELECT token, COUNT(*) FROM read_parquet(?) GROUP BY 1 ORDER BY 1",
                    [str(result.path)],
                ).fetchall()
            finally:
                dcon.close()
            _assert(rows == [(20, 5), (21, 5)], f"rows={rows}")

            st20 = catalog.get_fetch_state(con, 20, "day")
            _assert(st20 is not None and st20.status == "done", f"status={st20}")
            _assert(st20.interval == "day", "interval=day")
            # Minute state untouched
            _assert(catalog.get_fetch_state(con, 20, "minute") is None, "no minute state")

            calls_before = kite.calls
            result2 = fetch_mod.process_series(
                client,
                con,
                series,
                states,
                now=now,
                interval="day",
                series_root=day_dir,
                staging_dir=staging,
            )
            _assert(kite.calls == calls_before, "rerun makes 0 hist calls")
            _assert(result2.fetched == 0, "nothing fetched on rerun")
            _assert(result2.skipped == 2, f"skipped={result2.skipped}")

            loaded = read_mod.day_series("NIFTY", expiry, day_dir=day_dir)
            _assert(len(loaded) == 10, f"day_series rows={len(loaded)}")
        finally:
            con.close()


def test_fetch_minute_days_all_skips_without_traded_days() -> None:
    """--days all with day parquet present but volume=0 → skip minute (no hist calls)."""
    now = datetime(2026, 10, 25, 18, 0, 0, tzinfo=auth.IST)
    expiry = date(2026, 10, 27)

    def handler(token: int, from_date: Any, to_date: Any, interval: str, **kwargs: Any) -> list:
        raise AssertionError(f"unexpected historical call interval={interval}")

    client, kite, _clock = _make_client(handler)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        day_dir = root / "day"
        minute_dir = root / "minute"
        staging = root / "staging"
        con = catalog.connect(root / "c.duckdb")
        try:
            catalog.ensure_schema(con)
            series = _seed_series_contracts(
                con, underlying="NIFTY", expiry=expiry, tokens=[40], seen_on=now.date()
            )
            # Write day parquet with zero volume for token 40.
            dest = fetch_mod.series_path(day_dir, "NIFTY", expiry)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dcon = duckdb.connect()
            try:
                dcon.execute(
                    """
                    COPY (
                        SELECT
                            'NIFTY' AS underlying,
                            DATE '2026-10-24' AS expiry,
                            40::BIGINT AS token,
                            'X' AS tradingsymbol,
                            'CE' AS instrument_type,
                            100.0::DOUBLE AS strike,
                            75::INTEGER AS lot_size,
                            TIMESTAMP '2026-10-24 00:00:00' AS ts,
                            1.0::DOUBLE AS open,
                            1.0::DOUBLE AS high,
                            1.0::DOUBLE AS low,
                            1.0::DOUBLE AS close,
                            0::BIGINT AS volume,
                            0::BIGINT AS oi
                    ) TO ? (FORMAT parquet, COMPRESSION zstd)
                    """,
                    [str(dest)],
                )
            finally:
                dcon.close()
            traded = fetch_mod.traded_day_dates("NIFTY", expiry, 40, day_dir=day_dir)
            _assert(traded == set(), f"traded={traded}")

            states: dict[int, catalog.FetchState] = {}
            result = fetch_mod.process_series(
                client,
                con,
                series,
                states,
                now=now,
                interval="minute",
                series_root=minute_dir,
                day_dir=day_dir,
                staging_dir=staging,
                backfill_days=None,
            )
            _assert(kite.calls == 0, f"hist calls={kite.calls}")
            _assert(result.skipped == 1, f"skipped={result.skipped}")
            _assert(result.fetched == 0, "fetched")
            st = catalog.get_fetch_state(con, 40, "minute")
            _assert(st is not None and st.status == "done", f"status={st}")
        finally:
            con.close()


def test_fetch_minute_days_wide_multi_chunk() -> None:
    """backfill_days=120 yields two 60d historical requests per contract."""
    now = datetime(2026, 10, 25, 18, 0, 0, tzinfo=auth.IST)
    expiry = date(2026, 10, 27)
    spans: list[tuple[Any, Any]] = []

    def handler(token: int, from_date: Any, to_date: Any, interval: str, **kwargs: Any) -> list:
        _assert(interval == "minute", f"interval={interval}")
        spans.append((from_date, to_date))
        return _synth_candles(token, from_date, to_date, n=2, price=1.0)

    client, kite, _clock = _make_client(handler)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        con = catalog.connect(root / "c.duckdb")
        try:
            catalog.ensure_schema(con)
            series = _seed_series_contracts(
                con, underlying="NIFTY", expiry=expiry, tokens=[41], seen_on=now.date()
            )
            result = fetch_mod.process_series(
                client,
                con,
                series,
                {},
                now=now,
                interval="minute",
                series_root=root / "minute",
                staging_dir=root / "staging",
                backfill_days=120,
            )
            _assert(result.fetched == 1, f"fetched={result.fetched}")
            _assert(kite.calls == 2, f"hist calls={kite.calls}")
            _assert(len(spans) == 2, f"spans={len(spans)}")
        finally:
            con.close()


def test_fetch_combined_day_then_minute_order() -> None:
    """run_fetch day pass then minute pass: all day hist calls precede minute calls."""
    now = datetime(2026, 10, 25, 18, 0, 0, tzinfo=auth.IST)
    expiry = date(2026, 10, 27)
    call_intervals: list[str] = []

    def handler(token: int, from_date: Any, to_date: Any, interval: str, **kwargs: Any) -> list:
        call_intervals.append(interval)
        if interval == "day":
            return _synth_day_candles(token, from_date, to_date, n=3, price=float(token))
        return _synth_candles(token, from_date, to_date, n=2, price=float(token))

    client, kite, _clock = _make_client(handler)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        day_dir = root / "day"
        minute_dir = root / "minute"
        staging = root / "staging"
        con = catalog.connect(root / "c.duckdb")
        try:
            catalog.ensure_schema(con)
            series = _seed_series_contracts(
                con, underlying="NIFTY", expiry=expiry, tokens=[50, 51], seen_on=now.date()
            )
            queue = [series]
            day_states: dict[int, catalog.FetchState] = {}
            minute_states: dict[int, catalog.FetchState] = {}
            run_day = fetch_mod.run_fetch(
                client,
                con,
                queue,
                day_states,
                now=now,
                interval="day",
                series_root=day_dir,
                day_dir=day_dir,
                staging_dir=staging,
            )
            run_min = fetch_mod.run_fetch(
                client,
                con,
                queue,
                minute_states,
                now=now,
                interval="minute",
                series_root=minute_dir,
                day_dir=day_dir,
                staging_dir=staging,
                backfill_days=60,
            )
            _assert(run_day.contracts_fetched == 2, f"day fetched={run_day.contracts_fetched}")
            _assert(run_min.contracts_fetched == 2, f"min fetched={run_min.contracts_fetched}")
            _assert(kite.calls == 4, f"calls={kite.calls}")
            _assert(
                call_intervals == ["day", "day", "minute", "minute"],
                f"order={call_intervals}",
            )
            # Day-informed: traded days readable after day pass
            traded = fetch_mod.traded_day_dates("NIFTY", expiry, 50, day_dir=day_dir)
            _assert(traded is not None and len(traded) > 0, f"traded={traded}")
        finally:
            con.close()


def test_fetch_day_excludes_today_before_close() -> None:
    """During session, day_fetch_end is yesterday — today bars must not be stored."""
    now = datetime(2026, 10, 24, 12, 0, 0, tzinfo=auth.IST)  # Friday midday
    expiry = date(2026, 10, 27)

    def handler(token: int, from_date: Any, to_date: Any, interval: str, **kwargs: Any) -> list:
        # Return yesterday + today day bars; today must be dropped.
        yday = date(2026, 10, 23)
        today = date(2026, 10, 24)
        out = []
        for day, vol in ((yday, 50), (today, 99)):
            out.append(
                {
                    "date": datetime(day.year, day.month, day.day, tzinfo=auth.IST),
                    "open": 1.0,
                    "high": 2.0,
                    "low": 0.5,
                    "close": 1.5,
                    "volume": vol,
                    "oi": 10,
                }
            )
        return out

    client, _kite, _clock = _make_client(handler)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        con = catalog.connect(root / "c.duckdb")
        try:
            catalog.ensure_schema(con)
            series = _seed_series_contracts(
                con, underlying="NIFTY", expiry=expiry, tokens=[30], seen_on=now.date()
            )
            # Pretend we already have a watermark through day before yesterday so
            # the pending window is short (yesterday..end).
            states = {
                30: catalog.FetchState(
                    token=30,
                    interval="day",
                    fetched_to=datetime(2026, 10, 22, 15, 40, 0),
                    status="done",
                    rows_total=1,
                    attempts=1,
                    last_error=None,
                    updated_at=None,
                )
            }
            result = fetch_mod.process_series(
                client,
                con,
                series,
                states,
                now=now,
                interval="day",
                series_root=root / "day",
                staging_dir=root / "staging",
            )
            _assert(result.fetched == 1, f"fetched={result.fetched}")
            dcon = duckdb.connect()
            try:
                vols = dcon.execute(
                    "SELECT volume FROM read_parquet(?) ORDER BY ts",
                    [str(result.path)],
                ).fetchall()
            finally:
                dcon.close()
            # Only yesterday's bar (vol 50); today's vol 99 must be absent.
            _assert(any(v[0] == 50 for v in vols), f"vols={vols}")
            _assert(all(v[0] != 99 for v in vols), f"today leaked: {vols}")
            st = catalog.get_fetch_state(con, 30, "day")
            _assert(st is not None and st.status == "done", f"status={st}")
            _assert(st.fetched_to is not None and st.fetched_to.date() == date(2026, 10, 23), f"wm={st.fetched_to}")
        finally:
            con.close()


def test_fetch_backfill_and_rerun() -> None:
    now = datetime(2026, 10, 25, 18, 0, 0, tzinfo=auth.IST)
    expiry = date(2026, 10, 27)

    def handler(token: int, from_date: Any, to_date: Any, interval: str, **kwargs: Any) -> list:
        return _synth_candles(token, from_date, to_date, n=5, price=float(token))

    client, kite, _clock = _make_client(handler)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db = root / "c.duckdb"
        minute_dir = root / "minute"
        staging = root / "staging"
        con = catalog.connect(db)
        try:
            catalog.ensure_schema(con)
            series = _seed_series_contracts(
                con, underlying="NIFTY", expiry=expiry, tokens=[10, 11], seen_on=now.date()
            )
            states: dict[int, catalog.FetchState] = {}
            result = fetch_mod.process_series(
                client,
                con,
                series,
                states,
                now=now,
                minute_dir=minute_dir,
                staging_dir=staging,
            )
            _assert(not result.locked, "not locked")
            _assert(result.fetched == 2, f"fetched={result.fetched}")
            _assert(result.path is not None and result.path.is_file(), "parquet written")
            _assert(kite.calls == 2, f"hist calls={kite.calls}")

            # Sorted, deduped content
            dcon = duckdb.connect()
            try:
                rows = dcon.execute(
                    "SELECT token, COUNT(*) FROM read_parquet(?) GROUP BY 1 ORDER BY 1",
                    [str(result.path)],
                ).fetchall()
            finally:
                dcon.close()
            _assert(rows == [(10, 5), (11, 5)], f"rows={rows}")

            st10 = catalog.get_fetch_state(con, 10, "minute")
            _assert(st10 is not None and st10.status == "done", f"status={st10}")
            _assert(st10.fetched_to is not None, "watermark set")

            # Rerun → 0 historical calls
            calls_before = kite.calls
            result2 = fetch_mod.process_series(
                client,
                con,
                series,
                states,
                now=now,
                minute_dir=minute_dir,
                staging_dir=staging,
            )
            _assert(kite.calls == calls_before, "rerun makes 0 hist calls")
            _assert(result2.fetched == 0, "nothing fetched on rerun")
            _assert(result2.skipped == 2, f"skipped={result2.skipped}")
        finally:
            con.close()


def test_fetch_partial_rollback_and_dedupe() -> None:
    expiry = date(2026, 10, 27)
    # Wednesday — must be a weekday so skip-rule weekdays are non-empty.
    intraday = datetime(2026, 10, 21, 12, 0, 0, tzinfo=auth.IST)
    after = datetime(2026, 10, 21, 18, 0, 0, tzinfo=auth.IST)
    call_n = {"n": 0}

    def handler(token: int, from_date: Any, to_date: Any, interval: str, **kwargs: Any) -> list:
        call_n["n"] += 1
        # Put bars on the to_date session day so intraday + after-hours overlap.
        n = 2 if call_n["n"] == 1 else 4
        return _synth_candles(token, to_date, to_date, n=n, price=50.0)

    client, kite, _ = _make_client(handler)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        con = catalog.connect(root / "c.duckdb")
        try:
            catalog.ensure_schema(con)
            series = _seed_series_contracts(
                con, underlying="RELIANCE", expiry=expiry, tokens=[20], seen_on=intraday.date()
            )
            states: dict[int, catalog.FetchState] = {}
            r1 = fetch_mod.process_series(
                client,
                con,
                series,
                states,
                now=intraday,
                minute_dir=root / "minute",
                staging_dir=root / "staging",
            )
            st = catalog.get_fetch_state(con, 20, "minute")
            _assert(st is not None and st.status == "partial", f"partial status={st}")
            _assert(r1.rows == 2, f"rows1={r1.rows}")

            r2 = fetch_mod.process_series(
                client,
                con,
                series,
                states,
                now=after,
                minute_dir=root / "minute",
                staging_dir=root / "staging",
            )
            st2 = catalog.get_fetch_state(con, 20, "minute")
            _assert(st2 is not None and st2.status == "done", f"done status={st2}")
            dcon = duckdb.connect()
            try:
                n = dcon.execute(
                    "SELECT COUNT(*) FROM read_parquet(?)", [str(r2.path)]
                ).fetchone()[0]
                dup = dcon.execute(
                    """
                    SELECT COUNT(*) FROM (
                        SELECT token, ts, COUNT(*) c FROM read_parquet(?)
                        GROUP BY 1, 2 HAVING COUNT(*) > 1
                    )
                    """,
                    [str(r2.path)],
                ).fetchone()[0]
            finally:
                dcon.close()
            _assert(n == 4, f"deduped rows={n}")
            _assert(dup == 0, "no duplicate (token,ts)")
            _assert(kite.calls == 2, f"calls={kite.calls}")
        finally:
            con.close()


def test_fetch_skip_untraded_and_missing_snapshot() -> None:
    # Friday evening — window weekdays are Thu 22 + Fri 23.
    now = datetime(2026, 10, 23, 18, 0, 0, tzinfo=auth.IST)
    expiry = date(2026, 10, 27)
    watermark = datetime(2026, 10, 21, 15, 40, 0)  # end of Wednesday

    def handler(token: int, from_date: Any, to_date: Any, interval: str, **kwargs: Any) -> list:
        return _synth_candles(token, from_date, to_date, n=1)

    client, kite, _ = _make_client(handler)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        daily_dir = root / "daily"
        con = catalog.connect(root / "c.duckdb")
        try:
            catalog.ensure_schema(con)
            series = _seed_series_contracts(
                con, underlying="INFY", expiry=expiry, tokens=[30, 31], seen_on=now.date()
            )
            for t in (30, 31):
                catalog.upsert_fetch_state(
                    con,
                    catalog.FetchState(
                        token=t,
                        interval="minute",
                        fetched_to=watermark,
                        status="pending",
                        rows_total=1,
                        attempts=1,
                        last_error=None,
                        updated_at=watermark,
                    ),
                )
            states = catalog.list_fetch_states(con, interval="minute", tokens=[30, 31])

            for day, vols in (
                (date(2026, 10, 22), {30: 0, 31: 100}),
                (date(2026, 10, 23), {30: 0, 31: 50}),
            ):
                rows = [
                    _snap_row(30, "INFY", expiry, day, volume=vols[30]),
                    _snap_row(31, "INFY", expiry, day, volume=vols[31]),
                ]
                _write_final_snapshot(con, day, rows, daily_dir)

            with mock.patch.object(
                fetch_mod,
                "untraded_tokens",
                side_effect=lambda d, **kw: snapshot.untraded_tokens(d, dest_dir=daily_dir),
            ):
                r = fetch_mod.process_series(
                    client,
                    con,
                    series,
                    states,
                    now=now,
                    minute_dir=root / "minute",
                    staging_dir=root / "staging",
                )
            # Token 30 untraded every weekday → skip; 31 traded → fetch
            _assert(r.skipped == 1, f"skipped={r.skipped}")
            _assert(r.fetched == 1, f"fetched={r.fetched}")
            _assert(kite.calls == 1, f"calls={kite.calls}")
            st30 = catalog.get_fetch_state(con, 30, "minute")
            _assert(st30 is not None and st30.status == "done", "30 advanced without fetch")

            # Missing Friday snapshot → must fetch (leave only Thu 22)
            catalog.upsert_fetch_state(
                con,
                catalog.FetchState(
                    token=30,
                    interval="minute",
                    fetched_to=watermark,
                    status="pending",
                    rows_total=1,
                    attempts=1,
                    last_error=None,
                    updated_at=watermark,
                ),
            )
            con.execute("DELETE FROM daily_snapshots WHERE trade_date = ?", [date(2026, 10, 23)])
            (daily_dir / "20261023.parquet").unlink()
            states = catalog.list_fetch_states(con, interval="minute", tokens=[30, 31])
            kite.calls = 0
            with mock.patch.object(
                fetch_mod,
                "untraded_tokens",
                side_effect=lambda d, **kw: snapshot.untraded_tokens(d, dest_dir=daily_dir),
            ):
                r2 = fetch_mod.process_series(
                    client,
                    con,
                    series,
                    states,
                    now=now,
                    minute_dir=root / "minute",
                    staging_dir=root / "staging",
                )
            _assert(r2.fetched >= 1, f"missing snap forces fetch fetched={r2.fetched}")
            _assert(kite.calls >= 1, "hist called when snapshot missing")
        finally:
            con.close()


def test_fetch_session_expired_mid_series() -> None:
    now = datetime(2026, 10, 25, 18, 0, 0, tzinfo=auth.IST)
    expiry = date(2026, 10, 27)
    seen = {"n": 0}

    def handler(token: int, from_date: Any, to_date: Any, interval: str, **kwargs: Any) -> list:
        seen["n"] += 1
        if seen["n"] == 2:
            raise TokenException("token invalid")
        return _synth_candles(token, from_date, to_date, n=2)

    client, _, _ = _make_client(handler)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        con = catalog.connect(root / "c.duckdb")
        try:
            catalog.ensure_schema(con)
            series = _seed_series_contracts(
                con, underlying="NIFTY", expiry=expiry, tokens=[40, 41, 42], seen_on=now.date()
            )
            states: dict[int, catalog.FetchState] = {}
            try:
                fetch_mod.process_series(
                    client,
                    con,
                    series,
                    states,
                    now=now,
                    minute_dir=root / "minute",
                    staging_dir=root / "staging",
                )
                raise Failure("expected SessionExpired")
            except SessionExpired as exc:
                attached = getattr(exc, "result", None)
                _assert(attached is not None, "result attached")
                _assert(attached.fetched == 1, f"fetched before expire={attached.fetched}")
                st40 = catalog.get_fetch_state(con, 40, "minute")
                st41 = catalog.get_fetch_state(con, 41, "minute")
                st42 = catalog.get_fetch_state(con, 42, "minute")
                _assert(st40 is not None and st40.status == "done", "token 40 committed")
                _assert(st41 is None, "token 41 not committed")
                _assert(st42 is None, "token 42 not committed")
                path = series_path(root / "minute", "NIFTY", expiry)
                _assert(path.is_file(), "partial file written")
                dcon = duckdb.connect()
                try:
                    tokens = [
                        r[0]
                        for r in dcon.execute(
                            "SELECT DISTINCT token FROM read_parquet(?) ORDER BY 1",
                            [str(path)],
                        ).fetchall()
                    ]
                finally:
                    dcon.close()
                _assert(tokens == [40], f"only committed token in file: {tokens}")
        finally:
            con.close()


def test_fetch_lock_leaves_state_uncommitted() -> None:
    now = datetime(2026, 10, 25, 18, 0, 0, tzinfo=auth.IST)
    expiry = date(2026, 10, 27)

    def handler(token: int, from_date: Any, to_date: Any, interval: str, **kwargs: Any) -> list:
        return _synth_candles(token, from_date, to_date, n=2)

    client, _, _ = _make_client(handler)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        con = catalog.connect(root / "c.duckdb")
        try:
            catalog.ensure_schema(con)
            series = _seed_series_contracts(
                con, underlying="NIFTY", expiry=expiry, tokens=[50], seen_on=now.date()
            )
            states: dict[int, catalog.FetchState] = {}
            real_replace = os.replace

            def boom(src: Any, dst: Any) -> None:
                if str(dst).endswith(".parquet") and "minute" in str(dst).replace("\\", "/"):
                    raise PermissionError("locked")
                return real_replace(src, dst)

            with mock.patch.object(fetch_mod.os, "replace", side_effect=boom):
                result = fetch_mod.process_series(
                    client,
                    con,
                    series,
                    states,
                    now=now,
                    minute_dir=root / "minute",
                    staging_dir=root / "staging",
                )
            _assert(result.locked, "marked locked")
            _assert(catalog.get_fetch_state(con, 50, "minute") is None, "state not committed")
            dest = series_path(root / "minute", "NIFTY", expiry)
            _assert(not dest.is_file(), "dest not replaced")
        finally:
            con.close()


def test_fetch_gave_up_expired_contract() -> None:
    now = datetime(2026, 10, 25, 18, 0, 0, tzinfo=auth.IST)
    expiry = date(2026, 10, 20)  # expired

    def handler(token: int, from_date: Any, to_date: Any, interval: str, **kwargs: Any) -> list:
        raise InputException("invalid token")

    client, _, _ = _make_client(handler)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        con = catalog.connect(root / "c.duckdb")
        try:
            catalog.ensure_schema(con)
            series = _seed_series_contracts(
                con, underlying="OLD", expiry=expiry, tokens=[60], seen_on=date(2026, 10, 20)
            )
            states: dict[int, catalog.FetchState] = {}
            for _ in range(3):
                fetch_mod.process_series(
                    client,
                    con,
                    series,
                    states,
                    now=now,
                    minute_dir=root / "minute",
                    staging_dir=root / "staging",
                )
            st = catalog.get_fetch_state(con, 60, "minute")
            _assert(st is not None and st.status == "gave_up", f"gave_up status={st}")
            _assert(st.attempts >= 3, f"attempts={st.attempts}")
            _assert(planner.contract_pending_requests(st, now=now) == 0, "0 pending")
            # select_series should not keep unfinished expired with gave_up
            selected = universe.select_series(con, today=now.date())
            keys = {(s.underlying, s.expiry) for s in selected}
            _assert(("OLD", expiry) not in keys, "gave_up series not unfinished")
        finally:
            con.close()


def test_fetch_max_series() -> None:
    now = datetime(2026, 10, 25, 18, 0, 0, tzinfo=auth.IST)

    def handler(token: int, from_date: Any, to_date: Any, interval: str, **kwargs: Any) -> list:
        return _synth_candles(token, from_date, to_date, n=1)

    client, kite, _ = _make_client(handler)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        con = catalog.connect(root / "c.duckdb")
        try:
            catalog.ensure_schema(con)
            s1 = _seed_series_contracts(
                con, underlying="AAA", expiry=date(2026, 10, 27), tokens=[70], seen_on=now.date()
            )
            s2 = _seed_series_contracts(
                con, underlying="BBB", expiry=date(2026, 10, 27), tokens=[71], seen_on=now.date()
            )
            states: dict[int, catalog.FetchState] = {}
            run = fetch_mod.run_fetch(
                client,
                con,
                [s1, s2],
                states,
                now=now,
                max_series=1,
                minute_dir=root / "minute",
                staging_dir=root / "staging",
            )
            _assert(len(run.results) == 1, f"results={len(run.results)}")
            _assert(run.results[0].underlying == "AAA", "first series only")
            _assert(kite.calls == 1, f"calls={kite.calls}")
            _assert(catalog.get_fetch_state(con, 71, "minute") is None, "second not fetched")
        finally:
            con.close()


def test_setup_run_logging_writes_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "run.log"
        out = setup_run_logging("fetch", log_file=path)
        _assert(out == path, "returns path")
        log = __import__("logging").getLogger("ohlcv")
        log.info("hello-from-test")
        for h in list(log.handlers):
            h.flush()
            h.close()
        log.handlers.clear()
        text = path.read_text(encoding="utf-8")
        _assert("hello-from-test" in text, f"log contents={text!r}")
        _assert("log_file=" in text, "startup line")


def test_interrupt_soft_then_force() -> None:
    ctrl = InterruptController()
    announced: list[str] = []
    ctrl.install(on_soft_stop=lambda: announced.append("soft"))
    try:
        ctrl._handle(2, None)  # noqa: SLF001 — first Ctrl-C
        _assert(ctrl.soft_stop, "soft_stop set")
        _assert(ctrl.should_stop() == "keyboard interrupt", "should_stop")
        _assert(announced == ["soft"], f"announced={announced}")
        raised = False
        try:
            ctrl._handle(2, None)  # noqa: SLF001 — second Ctrl-C
        except SystemExit as exc:
            raised = True
            _assert(exc.code == 130, f"code={exc.code}")
        _assert(raised, "second Ctrl-C exits")
    finally:
        ctrl.restore()


def test_fetch_soft_interrupt_commits_and_stops() -> None:
    now = datetime(2026, 10, 25, 18, 0, 0, tzinfo=auth.IST)

    def handler(token: int, from_date: Any, to_date: Any, interval: str, **kwargs: Any) -> list:
        return _synth_candles(token, from_date, to_date, n=1)

    client, kite, _ = _make_client(handler)
    interrupt = InterruptController()
    seen: list[int] = []

    def on_contract(p: Any) -> None:
        seen.append(p.contract_index)
        # Soft-stop after first contract of first series.
        if len(seen) == 1:
            interrupt.soft_stop = True
            interrupt.reason = "keyboard interrupt"

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        con = catalog.connect(root / "c.duckdb")
        try:
            catalog.ensure_schema(con)
            s1 = _seed_series_contracts(
                con,
                underlying="AAA",
                expiry=date(2026, 10, 27),
                tokens=[80, 81],
                seen_on=now.date(),
            )
            s2 = _seed_series_contracts(
                con,
                underlying="BBB",
                expiry=date(2026, 10, 27),
                tokens=[82],
                seen_on=now.date(),
            )
            states: dict[int, catalog.FetchState] = {}
            run = fetch_mod.run_fetch(
                client,
                con,
                [s1, s2],
                states,
                now=now,
                minute_dir=root / "minute",
                staging_dir=root / "staging",
                on_contract=on_contract,
                interrupt=interrupt,
            )
            _assert(run.stopped_reason == "keyboard interrupt", f"stopped={run.stopped_reason}")
            _assert(len(run.results) == 1, f"results={len(run.results)}")
            _assert(run.results[0].fetched == 1, f"fetched={run.results[0].fetched}")
            _assert(run.results[0].stop_reason == "keyboard interrupt", "series stop")
            st80 = catalog.get_fetch_state(con, 80, "minute")
            st81 = catalog.get_fetch_state(con, 81, "minute")
            st82 = catalog.get_fetch_state(con, 82, "minute")
            _assert(st80 is not None and st80.status == "done", f"st80={st80}")
            _assert(st81 is None, "second contract of series1 not fetched")
            _assert(st82 is None, "series2 not started")
            dest = series_path(root / "minute", "AAA", date(2026, 10, 27))
            _assert(dest.is_file(), "series parquet committed")
        finally:
            con.close()


def _write_tiny_minute_parquet(
    *,
    contracts: list[catalog.ContractRow],
    spool_rows: list[dict[str, Any]],
    dest: Path,
    staging_dir: Path,
) -> int:
    """Write a tiny minute series parquet via fetch.merge_series_parquet."""
    staging_dir.mkdir(parents=True, exist_ok=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, spool_name = tempfile.mkstemp(prefix="ohlcv_spool_", suffix=".csv")
    os.close(fd)
    spool = Path(spool_name)
    try:
        import csv

        with open(spool, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=["token", "ts", "open", "high", "low", "close", "volume", "oi"],
            )
            writer.writeheader()
            for row in spool_rows:
                writer.writerow(row)
        return fetch_mod.merge_series_parquet(
            existing=None,
            spool_csv=spool,
            contracts=contracts,
            dest=dest,
            staging_dir=staging_dir,
        )
    finally:
        try:
            spool.unlink()
        except OSError:
            pass


def test_status_counts_and_pending() -> None:
    now = datetime(2026, 10, 25, 18, 0, 0, tzinfo=auth.IST)
    expiry = date(2026, 10, 27)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db = root / "c.duckdb"
        minute_dir = root / "minute"
        staging = root / "staging"
        con = catalog.connect(db)
        try:
            catalog.ensure_schema(con)
            series = _seed_series_contracts(
                con,
                underlying="NIFTY",
                expiry=expiry,
                tokens=[1, 2, 3, 4, 5],
                seen_on=now.date(),
            )
            # done / partial / error / gave_up / missing(no row)
            catalog.upsert_fetch_state(
                con,
                catalog.FetchState(
                    token=1,
                    interval="minute",
                    fetched_to=datetime(2026, 10, 25, 15, 40, 0),
                    status="done",
                    rows_total=10,
                    attempts=1,
                    last_error=None,
                    updated_at=now.replace(tzinfo=None),
                ),
            )
            catalog.upsert_fetch_state(
                con,
                catalog.FetchState(
                    token=2,
                    interval="minute",
                    fetched_to=datetime(2026, 10, 24, 12, 0, 0),
                    status="partial",
                    rows_total=5,
                    attempts=1,
                    last_error=None,
                    updated_at=now.replace(tzinfo=None),
                ),
            )
            catalog.upsert_fetch_state(
                con,
                catalog.FetchState(
                    token=3,
                    interval="minute",
                    fetched_to=None,
                    status="error",
                    rows_total=0,
                    attempts=2,
                    last_error="boom",
                    updated_at=now.replace(tzinfo=None),
                ),
            )
            catalog.upsert_fetch_state(
                con,
                catalog.FetchState(
                    token=4,
                    interval="minute",
                    fetched_to=None,
                    status="gave_up",
                    rows_total=0,
                    attempts=3,
                    last_error="gone",
                    updated_at=now.replace(tzinfo=None),
                ),
            )
            # token 5 = missing

            rows = status_mod.series_status(
                con, now=now, minute_dir=minute_dir
            )
            nifty = next(r for r in rows if r.underlying == "NIFTY" and r.expiry == expiry)
            _assert(nifty.contracts == 5, f"contracts={nifty.contracts}")
            _assert(nifty.done == 1, f"done={nifty.done}")
            _assert(nifty.partial == 1, f"partial={nifty.partial}")
            _assert(nifty.error == 1, f"error={nifty.error}")
            _assert(nifty.gave_up == 1, f"gave_up={nifty.gave_up}")
            _assert(nifty.missing == 1, f"missing={nifty.missing}")
            # complete = done (watermark at target) + gave_up
            _assert(nifty.complete == 2, f"complete={nifty.complete}")
            _assert(nifty.pending_requests >= 1, f"pending={nifty.pending_requests}")

            # Write a tiny file and check file_rows via status.
            n = _write_tiny_minute_parquet(
                contracts=list(series.contracts),
                spool_rows=[
                    {
                        "token": 1,
                        "ts": "2026-10-24 09:15:00",
                        "open": 1,
                        "high": 2,
                        "low": 1,
                        "close": 1.5,
                        "volume": 10,
                        "oi": 100,
                    },
                    {
                        "token": 1,
                        "ts": "2026-10-24 09:16:00",
                        "open": 1.5,
                        "high": 2,
                        "low": 1.4,
                        "close": 1.6,
                        "volume": 5,
                        "oi": 101,
                    },
                ],
                dest=series_path(minute_dir, "NIFTY", expiry),
                staging_dir=staging,
            )
            _assert(n == 2, f"rows written={n}")
            rows2 = status_mod.series_status(con, now=now, minute_dir=minute_dir)
            nifty2 = next(r for r in rows2 if r.underlying == "NIFTY")
            _assert(nifty2.file_rows == 2, f"file_rows={nifty2.file_rows}")
            _assert(nifty2.file_bytes > 0, "file_bytes")
            tot = status_mod.totals(rows2)
            _assert(tot.series >= 1, "totals series")
            _assert(tot.file_bytes == nifty2.file_bytes, "totals bytes")
        finally:
            con.close()


def test_status_include_all_maps_sanitized_folder() -> None:
    now = datetime(2026, 10, 25, 18, 0, 0, tzinfo=auth.IST)
    # Expired + fully done so it drops out of default select_series unfinished set.
    expired = date(2026, 10, 20)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db = root / "c.duckdb"
        minute_dir = root / "minute"
        staging = root / "staging"
        con = catalog.connect(db)
        try:
            catalog.ensure_schema(con)
            series = _seed_series_contracts(
                con,
                underlying="M&M",
                expiry=expired,
                tokens=[90],
                seen_on=expired,
            )
            catalog.upsert_fetch_state(
                con,
                catalog.FetchState(
                    token=90,
                    interval="minute",
                    fetched_to=datetime(2026, 10, 20, 15, 40, 0),
                    status="done",
                    rows_total=2,
                    attempts=1,
                    last_error=None,
                    updated_at=now.replace(tzinfo=None),
                ),
            )
            dest = series_path(minute_dir, "M&M", expired)
            _assert("M_M" in str(dest), f"sanitized path={dest}")
            _write_tiny_minute_parquet(
                contracts=list(series.contracts),
                spool_rows=[
                    {
                        "token": 90,
                        "ts": "2026-10-20 09:15:00",
                        "open": 1,
                        "high": 1,
                        "low": 1,
                        "close": 1,
                        "volume": 1,
                        "oi": 1,
                    }
                ],
                dest=dest,
                staging_dir=staging,
            )

            default_rows = status_mod.series_status(con, now=now, minute_dir=minute_dir)
            _assert(
                not any(r.underlying == "M&M" and r.expiry == expired for r in default_rows),
                "finished expired excluded from default",
            )
            all_rows = status_mod.series_status(
                con, now=now, include_all=True, minute_dir=minute_dir
            )
            mm = next(r for r in all_rows if r.expiry == expired)
            _assert(mm.underlying == "M&M", f"mapped underlying={mm.underlying}")
            _assert(mm.file_rows == 1, f"file_rows={mm.file_rows}")
            _assert(mm.live is False, "expired not live")
        finally:
            con.close()


def test_snapshot_gaps_weekdays_only() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "c.duckdb"
        con = catalog.connect(db)
        try:
            catalog.ensure_schema(con)
            # Calendar (2026): Mon 19, Tue 20, Wed 21, Thu 22, Fri 23, Sat 24, Sun 25.
            # Snapshots on Tue 20 + Thu 22; gaps should be Wed 21 + Fri 23.
            for d, status in [
                (date(2026, 10, 20), "final"),
                (date(2026, 10, 22), "final"),
            ]:
                catalog.record_snapshot(
                    con,
                    catalog.DailySnapshotMeta(
                        trade_date=d,
                        status=status,
                        taken_at=datetime(d.year, d.month, d.day, 16, 0),
                        contracts=1,
                        traded_contracts=1,
                        missing=0,
                        total_volume=1,
                        path=f"{ymd(d)}.parquet",
                    ),
                )
            # Fri 2026-10-23 after session → last = Fri (include Fri as a gap).
            gaps = status_mod.snapshot_gaps(
                con,
                today=date(2026, 10, 23),
                now=datetime(2026, 10, 23, 16, 0, 0, tzinfo=auth.IST),
            )
            _assert(date(2026, 10, 21) in gaps, f"wed missing in {gaps}")
            _assert(date(2026, 10, 23) in gaps, f"fri missing in {gaps}")
            _assert(date(2026, 10, 24) not in gaps, "weekend sat not included")
            _assert(date(2026, 10, 25) not in gaps, "weekend sun not included")
            _assert(date(2026, 10, 20) not in gaps, "present tue not a gap")
            _assert(date(2026, 10, 22) not in gaps, "present thu not a gap")
            # Before 15:40 on Fri → last = Thu (Fri not yet expected).
            gaps_am = status_mod.snapshot_gaps(
                con,
                today=date(2026, 10, 23),
                now=datetime(2026, 10, 23, 12, 0, 0, tzinfo=auth.IST),
            )
            _assert(date(2026, 10, 23) not in gaps_am, f"fri before close in {gaps_am}")
            _assert(date(2026, 10, 21) in gaps_am, "wed still a gap before close")
        finally:
            con.close()


def test_read_series_filters_and_file_info() -> None:
    expiry = date(2026, 10, 27)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        minute_dir = root / "minute"
        staging = root / "staging"
        contracts = [
            catalog.ContractRow(
                1, "NIFTY", expiry, "NIFTY1", "FUT", 0.0, 50
            ),
            catalog.ContractRow(
                2, "NIFTY", expiry, "NIFTY2", "CE", 25000.0, 50
            ),
        ]
        dest = series_path(minute_dir, "NIFTY", expiry)
        _write_tiny_minute_parquet(
            contracts=contracts,
            spool_rows=[
                {
                    "token": 1,
                    "ts": "2026-10-24 09:15:00",
                    "open": 100,
                    "high": 101,
                    "low": 99,
                    "close": 100.5,
                    "volume": 10,
                    "oi": 1,
                },
                {
                    "token": 1,
                    "ts": "2026-10-24 09:16:00",
                    "open": 100.5,
                    "high": 102,
                    "low": 100,
                    "close": 101,
                    "volume": 20,
                    "oi": 2,
                },
                {
                    "token": 2,
                    "ts": "2026-10-24 09:15:00",
                    "open": 50,
                    "high": 51,
                    "low": 49,
                    "close": 50.5,
                    "volume": 5,
                    "oi": 3,
                },
            ],
            dest=dest,
            staging_dir=staging,
        )
        size, rows, min_ts, max_ts = read_mod.file_info(dest)
        _assert(size > 0, "size")
        _assert(rows == 3, f"rows={rows}")
        _assert(min_ts is not None and max_ts is not None, "ts span")
        _assert(min_ts <= max_ts, "min<=max")

        by_token = read_mod.series(
            "NIFTY", expiry, tokens=[1], minute_dir=minute_dir
        )
        _assert(len(by_token) == 2, f"token filter={len(by_token)}")
        _assert(all(r["token"] == 1 for r in by_token), "all token 1")

        by_sym = read_mod.series(
            "NIFTY", expiry, symbols=["NIFTY2"], minute_dir=minute_dir
        )
        _assert(len(by_sym) == 1, f"symbol filter={len(by_sym)}")
        _assert(by_sym[0]["tradingsymbol"] == "NIFTY2", "symbol")

        by_time = read_mod.series(
            "NIFTY",
            expiry,
            start=datetime(2026, 10, 24, 9, 16, 0),
            end=datetime(2026, 10, 24, 9, 16, 0),
            minute_dir=minute_dir,
        )
        _assert(len(by_time) == 1, f"time filter={len(by_time)}")

        raised = False
        try:
            read_mod.series("NIFTY", date(2026, 1, 1), minute_dir=minute_dir)
        except FileNotFoundError:
            raised = True
        _assert(raised, "missing series raises")

        raised2 = False
        try:
            read_mod.file_info(root / "nope.parquet")
        except FileNotFoundError:
            raised2 = True
        _assert(raised2, "missing file_info raises")

        # underlying_minute glob
        all_rows = read_mod.underlying_minute(
            "NIFTY", instrument_type="CE", minute_dir=minute_dir
        )
        _assert(len(all_rows) == 1, f"underlying CE={len(all_rows)}")


def test_catalog_readonly_open() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "c.duckdb"
        con = catalog.connect(db)
        try:
            catalog.ensure_schema(con)
            catalog.start_run(
                con, "sync", started_at=datetime(2026, 10, 25, 10, 0, 0)
            )
            catalog.finish_run(
                con,
                1,
                finished_at=datetime(2026, 10, 25, 10, 1, 0),
                requests=0,
                note="ok",
            )
            catalog.record_snapshot(
                con,
                catalog.DailySnapshotMeta(
                    trade_date=date(2026, 10, 24),
                    status="final",
                    taken_at=datetime(2026, 10, 24, 16, 0),
                    contracts=1,
                    traded_contracts=1,
                    missing=0,
                    total_volume=1,
                    path="x.parquet",
                ),
            )
        finally:
            con.close()

        ro = catalog.connect(db, read_only=True)
        try:
            runs = catalog.list_runs(ro, limit=5)
            _assert(len(runs) == 1, f"runs={len(runs)}")
            _assert(runs[0].command == "sync", "command")
            _assert(runs[0].note == "ok", "note")
            snaps = catalog.list_snapshots(ro, limit=5)
            _assert(len(snaps) == 1, f"snaps={len(snaps)}")
            _assert(snaps[0].trade_date == date(2026, 10, 24), "snap date")
        finally:
            ro.close()


def main() -> None:
    tests = [
        test_paths,
        test_markets_and_paths_for,
        test_planner_fetch_end_per_market,
        test_fetch_chunk_windows_mcx_session,
        test_fetch_mcx_day_uses_market_session,
        test_archive_idempotent,
        test_catalog_schema,
        test_limiter_spacing,
        test_retry_ladder_and_caps,
        test_rate_limit_min_wait,
        test_input_exception_not_retried,
        test_token_exception_session_expired,
        test_universe_nearest3_and_unfinished,
        test_sync_mcx_cds_options_and_types_override,
        test_dual_nearest3_fut_and_opt_pools,
        test_build_queue_types_filter_excludes_fut,
        test_queue_order_and_chunks,
        test_catalog_v1_to_v2_migration,
        test_snapshot_status_boundary,
        test_snapshot_mcx_quote_prefix_and_rank,
        test_continuous_nearest_fut_and_write,
        test_can_skip_untraded_uses_market_daily_dir,
        test_snapshot_happy_path_and_fields,
        test_snapshot_rejects_weekend_and_batch_failure,
        test_snapshot_missing_keys_and_rank,
        test_fetch_day_backfill_and_rerun,
        test_fetch_minute_days_all_skips_without_traded_days,
        test_fetch_minute_days_wide_multi_chunk,
        test_fetch_combined_day_then_minute_order,
        test_fetch_day_excludes_today_before_close,
        test_fetch_backfill_and_rerun,
        test_fetch_partial_rollback_and_dedupe,
        test_fetch_skip_untraded_and_missing_snapshot,
        test_fetch_session_expired_mid_series,
        test_fetch_lock_leaves_state_uncommitted,
        test_fetch_gave_up_expired_contract,
        test_fetch_max_series,
        test_setup_run_logging_writes_file,
        test_interrupt_soft_then_force,
        test_fetch_soft_interrupt_commits_and_stops,
        test_status_counts_and_pending,
        test_status_include_all_maps_sanitized_folder,
        test_snapshot_gaps_weekdays_only,
        test_read_series_filters_and_file_info,
        test_catalog_readonly_open,
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
