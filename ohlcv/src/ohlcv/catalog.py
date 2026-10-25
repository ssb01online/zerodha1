"""DuckDB catalog for OHLCV contracts, fetch watermarks, ranks, and runs."""

from __future__ import annotations

import csv
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import duckdb

from .paths import CATALOG_PATH, ensure_dir

SCHEMA_VERSION = 2

_LOCK_HINT = (
    "catalog.duckdb is locked — another ohlcv process or notebook has it open. "
    "Close the other connection and retry."
)


@dataclass(frozen=True)
class ContractRow:
    token: int
    underlying: str
    expiry: date
    tradingsymbol: str
    instrument_type: str
    strike: float
    lot_size: int
    first_seen: date | None = None
    last_seen: date | None = None


@dataclass(frozen=True)
class FetchState:
    token: int
    interval: str
    fetched_to: datetime | None
    status: str
    rows_total: int
    attempts: int
    last_error: str | None
    updated_at: datetime | None


@dataclass(frozen=True)
class UnderlyingRank:
    trade_date: date
    underlying: str
    fut_token: int
    volume: int
    close: float
    traded_value: float
    source: str


@dataclass(frozen=True)
class DailySnapshotMeta:
    trade_date: date
    status: str
    taken_at: datetime
    contracts: int
    traded_contracts: int
    missing: int
    total_volume: int
    path: str


class CatalogLocked(RuntimeError):
    """Raised when DuckDB cannot open the catalog due to a file lock."""


def connect(db_path: Path | None = None, *, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    path = Path(db_path) if db_path is not None else CATALOG_PATH
    if not read_only:
        ensure_dir(path.parent)
    try:
        return duckdb.connect(str(path), read_only=read_only)
    except duckdb.IOException as exc:
        msg = str(exc).lower()
        if "lock" in msg or "conflicting" in msg:
            raise CatalogLocked(_LOCK_HINT) from exc
        raise


def _migrate_schema(con: duckdb.DuckDBPyConnection, stored: int) -> None:
    if stored == SCHEMA_VERSION:
        return
    if stored == 1 and SCHEMA_VERSION == 2:
        # daily_snapshots is created below via CREATE TABLE IF NOT EXISTS.
        con.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            [str(SCHEMA_VERSION)],
        )
        return
    raise RuntimeError(
        f"catalog.duckdb schema_version={stored}, code expects {SCHEMA_VERSION}. "
        "Bump SCHEMA_VERSION and migrate, or delete the DB to rebuild."
    )


def ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    row = con.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        con.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
            [str(SCHEMA_VERSION)],
        )
    else:
        _migrate_schema(con, int(row[0]))

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS contracts (
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
        CREATE TABLE IF NOT EXISTS fetch_state (
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
        CREATE TABLE IF NOT EXISTS underlying_rank (
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
        CREATE TABLE IF NOT EXISTS runs (
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
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_snapshots (
            trade_date DATE PRIMARY KEY,
            status TEXT NOT NULL,
            taken_at TIMESTAMP NOT NULL,
            contracts INTEGER NOT NULL,
            traded_contracts INTEGER NOT NULL,
            missing INTEGER NOT NULL,
            total_volume BIGINT NOT NULL,
            path TEXT NOT NULL
        )
        """
    )


def schema_version(con: duckdb.DuckDBPyConnection) -> int:
    row = con.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    return int(row[0]) if row else 0


def upsert_contracts(
    con: duckdb.DuckDBPyConnection,
    rows: Iterable[ContractRow],
    *,
    seen_on: date,
) -> int:
    """Insert or update contracts; bump last_seen / preserve first_seen. Returns count upserted.

    Stages via a temp CSV then one INSERT...ON CONFLICT merge — DuckDB executemany is too slow for ~40k rows.
    """
    batch = list(rows)
    if not batch:
        return 0

    seen_s = seen_on.isoformat()
    fd, tmp_name = tempfile.mkstemp(prefix="ohlcv_contracts_", suffix=".csv")
    tmp_path = Path(tmp_name)
    try:
        with open(fd, "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(
                [
                    "token",
                    "underlying",
                    "expiry",
                    "tradingsymbol",
                    "instrument_type",
                    "strike",
                    "lot_size",
                    "first_seen",
                    "last_seen",
                ]
            )
            for row in batch:
                writer.writerow(
                    [
                        row.token,
                        row.underlying,
                        row.expiry.isoformat(),
                        row.tradingsymbol,
                        row.instrument_type,
                        row.strike,
                        row.lot_size,
                        seen_s,
                        seen_s,
                    ]
                )
        # first_seen is set only on INSERT; ON CONFLICT leaves the existing first_seen alone.
        con.execute(
            """
            INSERT INTO contracts (
                token, underlying, expiry, tradingsymbol, instrument_type,
                strike, lot_size, first_seen, last_seen
            )
            SELECT
                token,
                underlying,
                CAST(expiry AS DATE),
                tradingsymbol,
                instrument_type,
                strike,
                lot_size,
                CAST(first_seen AS DATE),
                CAST(last_seen AS DATE)
            FROM read_csv(?, header = true, auto_detect = true)
            ON CONFLICT (token) DO UPDATE SET
                underlying = excluded.underlying,
                expiry = excluded.expiry,
                tradingsymbol = excluded.tradingsymbol,
                instrument_type = excluded.instrument_type,
                strike = excluded.strike,
                lot_size = excluded.lot_size,
                last_seen = excluded.last_seen
            """,
            [str(tmp_path)],
        )
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass
    return len(batch)


def list_contracts(
    con: duckdb.DuckDBPyConnection,
    *,
    underlying: str | None = None,
    expiry: date | None = None,
) -> list[ContractRow]:
    clauses: list[str] = []
    params: list[Any] = []
    if underlying is not None:
        clauses.append("underlying = ?")
        params.append(underlying)
    if expiry is not None:
        clauses.append("expiry = ?")
        params.append(expiry)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = con.execute(
        f"""
        SELECT token, underlying, expiry, tradingsymbol, instrument_type,
               strike, lot_size, first_seen, last_seen
        FROM contracts
        {where}
        ORDER BY underlying, expiry, instrument_type, strike, token
        """,
        params,
    ).fetchall()
    return [
        ContractRow(
            token=int(r[0]),
            underlying=str(r[1]),
            expiry=r[2] if isinstance(r[2], date) else date.fromisoformat(str(r[2])[:10]),
            tradingsymbol=str(r[3]),
            instrument_type=str(r[4]),
            strike=float(r[5]),
            lot_size=int(r[6]),
            first_seen=r[7] if r[7] is None or isinstance(r[7], date) else date.fromisoformat(str(r[7])[:10]),
            last_seen=r[8] if r[8] is None or isinstance(r[8], date) else date.fromisoformat(str(r[8])[:10]),
        )
        for r in rows
    ]


def contract_count(con: duckdb.DuckDBPyConnection) -> int:
    row = con.execute("SELECT COUNT(*) FROM contracts").fetchone()
    return int(row[0]) if row else 0


def get_fetch_state(
    con: duckdb.DuckDBPyConnection,
    token: int,
    interval: str,
) -> FetchState | None:
    row = con.execute(
        """
        SELECT token, interval, fetched_to, status, rows_total, attempts, last_error, updated_at
        FROM fetch_state
        WHERE token = ? AND interval = ?
        """,
        [token, interval],
    ).fetchone()
    if row is None:
        return None
    return FetchState(
        token=int(row[0]),
        interval=str(row[1]),
        fetched_to=row[2],
        status=str(row[3]),
        rows_total=int(row[4] or 0),
        attempts=int(row[5] or 0),
        last_error=row[6],
        updated_at=row[7],
    )


def upsert_fetch_state(
    con: duckdb.DuckDBPyConnection,
    state: FetchState,
) -> None:
    """Insert or replace one fetch_state row."""
    con.execute(
        """
        INSERT INTO fetch_state (
            token, interval, fetched_to, status, rows_total, attempts, last_error, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (token, interval) DO UPDATE SET
            fetched_to = excluded.fetched_to,
            status = excluded.status,
            rows_total = excluded.rows_total,
            attempts = excluded.attempts,
            last_error = excluded.last_error,
            updated_at = excluded.updated_at
        """,
        [
            state.token,
            state.interval,
            state.fetched_to,
            state.status,
            state.rows_total,
            state.attempts,
            state.last_error,
            state.updated_at,
        ],
    )


def upsert_fetch_states(
    con: duckdb.DuckDBPyConnection,
    states: Iterable[FetchState],
) -> int:
    """Upsert many fetch_state rows. Returns count written."""
    count = 0
    for state in states:
        upsert_fetch_state(con, state)
        count += 1
    return count


def list_fetch_states(
    con: duckdb.DuckDBPyConnection,
    *,
    interval: str = "minute",
    tokens: Iterable[int] | None = None,
) -> dict[int, FetchState]:
    if tokens is None:
        rows = con.execute(
            """
            SELECT token, interval, fetched_to, status, rows_total, attempts, last_error, updated_at
            FROM fetch_state
            WHERE interval = ?
            """,
            [interval],
        ).fetchall()
    else:
        token_list = list(tokens)
        if not token_list:
            return {}
        placeholders = ",".join("?" * len(token_list))
        rows = con.execute(
            f"""
            SELECT token, interval, fetched_to, status, rows_total, attempts, last_error, updated_at
            FROM fetch_state
            WHERE interval = ? AND token IN ({placeholders})
            """,
            [interval, *token_list],
        ).fetchall()
    out: dict[int, FetchState] = {}
    for row in rows:
        state = FetchState(
            token=int(row[0]),
            interval=str(row[1]),
            fetched_to=row[2],
            status=str(row[3]),
            rows_total=int(row[4] or 0),
            attempts=int(row[5] or 0),
            last_error=row[6],
            updated_at=row[7],
        )
        out[state.token] = state
    return out


def upsert_underlying_ranks(con: duckdb.DuckDBPyConnection, ranks: Iterable[UnderlyingRank]) -> int:
    count = 0
    for r in ranks:
        con.execute(
            """
            INSERT INTO underlying_rank (
                trade_date, underlying, fut_token, volume, close, traded_value, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (trade_date, underlying) DO UPDATE SET
                fut_token = excluded.fut_token,
                volume = excluded.volume,
                close = excluded.close,
                traded_value = excluded.traded_value,
                source = excluded.source
            """,
            [
                r.trade_date,
                r.underlying,
                r.fut_token,
                r.volume,
                r.close,
                r.traded_value,
                r.source,
            ],
        )
        count += 1
    return count


def latest_rank_trade_date(con: duckdb.DuckDBPyConnection) -> date | None:
    row = con.execute("SELECT MAX(trade_date) FROM underlying_rank").fetchone()
    if row is None or row[0] is None:
        return None
    value = row[0]
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def list_underlying_ranks(
    con: duckdb.DuckDBPyConnection,
    trade_date: date | None = None,
) -> dict[str, UnderlyingRank]:
    if trade_date is None:
        trade_date = latest_rank_trade_date(con)
    if trade_date is None:
        return {}
    rows = con.execute(
        """
        SELECT trade_date, underlying, fut_token, volume, close, traded_value, source
        FROM underlying_rank
        WHERE trade_date = ?
        """,
        [trade_date],
    ).fetchall()
    out: dict[str, UnderlyingRank] = {}
    for row in rows:
        td = row[0] if isinstance(row[0], date) else date.fromisoformat(str(row[0])[:10])
        rank = UnderlyingRank(
            trade_date=td,
            underlying=str(row[1]),
            fut_token=int(row[2]),
            volume=int(row[3]),
            close=float(row[4]),
            traded_value=float(row[5]),
            source=str(row[6]),
        )
        out[rank.underlying] = rank
    return out


def _next_run_id(con: duckdb.DuckDBPyConnection) -> int:
    row = con.execute("SELECT COALESCE(MAX(run_id), 0) + 1 FROM runs").fetchone()
    return int(row[0]) if row else 1


def start_run(con: duckdb.DuckDBPyConnection, command: str, *, started_at: datetime) -> int:
    run_id = _next_run_id(con)
    con.execute(
        """
        INSERT INTO runs (run_id, command, started_at, finished_at, requests, retries, errors, note)
        VALUES (?, ?, ?, NULL, 0, 0, 0, NULL)
        """,
        [run_id, command, started_at],
    )
    return run_id


def finish_run(
    con: duckdb.DuckDBPyConnection,
    run_id: int,
    *,
    finished_at: datetime,
    requests: int = 0,
    retries: int = 0,
    errors: int = 0,
    note: str | None = None,
) -> None:
    con.execute(
        """
        UPDATE runs
        SET finished_at = ?, requests = ?, retries = ?, errors = ?, note = ?
        WHERE run_id = ?
        """,
        [finished_at, requests, retries, errors, note, run_id],
    )


def _as_date(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return date.fromisoformat(str(value)[:10])


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def record_snapshot(con: duckdb.DuckDBPyConnection, meta: DailySnapshotMeta) -> None:
    con.execute(
        """
        INSERT INTO daily_snapshots (
            trade_date, status, taken_at, contracts, traded_contracts,
            missing, total_volume, path
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (trade_date) DO UPDATE SET
            status = excluded.status,
            taken_at = excluded.taken_at,
            contracts = excluded.contracts,
            traded_contracts = excluded.traded_contracts,
            missing = excluded.missing,
            total_volume = excluded.total_volume,
            path = excluded.path
        """,
        [
            meta.trade_date,
            meta.status,
            meta.taken_at,
            meta.contracts,
            meta.traded_contracts,
            meta.missing,
            meta.total_volume,
            meta.path,
        ],
    )


def get_snapshot(
    con: duckdb.DuckDBPyConnection,
    trade_date: date,
) -> DailySnapshotMeta | None:
    row = con.execute(
        """
        SELECT trade_date, status, taken_at, contracts, traded_contracts,
               missing, total_volume, path
        FROM daily_snapshots
        WHERE trade_date = ?
        """,
        [trade_date],
    ).fetchone()
    if row is None:
        return None
    return DailySnapshotMeta(
        trade_date=_as_date(row[0]),
        status=str(row[1]),
        taken_at=_as_datetime(row[2]),
        contracts=int(row[3]),
        traded_contracts=int(row[4]),
        missing=int(row[5]),
        total_volume=int(row[6]),
        path=str(row[7]),
    )


def latest_snapshot(
    con: duckdb.DuckDBPyConnection,
    *,
    status: str | None = None,
) -> DailySnapshotMeta | None:
    if status is None:
        row = con.execute(
            """
            SELECT trade_date, status, taken_at, contracts, traded_contracts,
                   missing, total_volume, path
            FROM daily_snapshots
            ORDER BY trade_date DESC
            LIMIT 1
            """
        ).fetchone()
    else:
        row = con.execute(
            """
            SELECT trade_date, status, taken_at, contracts, traded_contracts,
                   missing, total_volume, path
            FROM daily_snapshots
            WHERE status = ?
            ORDER BY trade_date DESC
            LIMIT 1
            """,
            [status],
        ).fetchone()
    if row is None:
        return None
    return DailySnapshotMeta(
        trade_date=_as_date(row[0]),
        status=str(row[1]),
        taken_at=_as_datetime(row[2]),
        contracts=int(row[3]),
        traded_contracts=int(row[4]),
        missing=int(row[5]),
        total_volume=int(row[6]),
        path=str(row[7]),
    )


def previous_session(
    con: duckdb.DuckDBPyConnection,
    trade_date: date,
) -> date | None:
    """Return the latest snapshot trade_date strictly before ``trade_date``."""
    row = con.execute(
        """
        SELECT MAX(trade_date)
        FROM daily_snapshots
        WHERE trade_date < ?
        """,
        [trade_date],
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return _as_date(row[0])


@dataclass(frozen=True)
class RunRow:
    run_id: int
    command: str
    started_at: datetime
    finished_at: datetime | None
    requests: int
    retries: int
    errors: int
    note: str | None


def list_runs(
    con: duckdb.DuckDBPyConnection,
    *,
    limit: int = 10,
) -> list[RunRow]:
    """Return recent runs, newest first."""
    rows = con.execute(
        """
        SELECT run_id, command, started_at, finished_at, requests, retries, errors, note
        FROM runs
        ORDER BY run_id DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()
    out: list[RunRow] = []
    for row in rows:
        out.append(
            RunRow(
                run_id=int(row[0]),
                command=str(row[1]),
                started_at=_as_datetime(row[2]),
                finished_at=_as_datetime(row[3]) if row[3] is not None else None,
                requests=int(row[4] or 0),
                retries=int(row[5] or 0),
                errors=int(row[6] or 0),
                note=row[7],
            )
        )
    return out


def list_snapshots(
    con: duckdb.DuckDBPyConnection,
    *,
    limit: int | None = None,
) -> list[DailySnapshotMeta]:
    """Return daily snapshots ordered by trade_date descending."""
    if limit is None:
        rows = con.execute(
            """
            SELECT trade_date, status, taken_at, contracts, traded_contracts,
                   missing, total_volume, path
            FROM daily_snapshots
            ORDER BY trade_date DESC
            """
        ).fetchall()
    else:
        rows = con.execute(
            """
            SELECT trade_date, status, taken_at, contracts, traded_contracts,
                   missing, total_volume, path
            FROM daily_snapshots
            ORDER BY trade_date DESC
            LIMIT ?
            """,
            [limit],
        ).fetchall()
    return [
        DailySnapshotMeta(
            trade_date=_as_date(row[0]),
            status=str(row[1]),
            taken_at=_as_datetime(row[2]),
            contracts=int(row[3]),
            traded_contracts=int(row[4]),
            missing=int(row[5]),
            total_volume=int(row[6]),
            path=str(row[7]),
        )
        for row in rows
    ]
