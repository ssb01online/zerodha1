"""DuckDB persistence for sparse portfolio composition snapshots.

Single-writer assumption: only one process should open the DB for writes
at a time (Phase 2 will make the API the sole writer). Path defaults to
data/portfolio.duckdb (gitignored via data/).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb

from kitetools.config import DATA_DIR

from . import fingerprint as fp
from .fetch import RawPayload
from .models import Holding, MfHolding, PortfolioView, Position
from .normalize import to_view

SCHEMA_VERSION = 1
DEFAULT_DB_PATH = DATA_DIR / "portfolio.duckdb"


@dataclass(frozen=True)
class CaptureResult:
    saved: bool
    snapshot_id: int | None
    fingerprint_hash: str
    diff: fp.CompositionDiff


@dataclass(frozen=True)
class SnapshotMeta:
    id: int
    observed_at: datetime
    effective_date: date | None
    source: str
    fingerprint_hash: str
    counts: dict[str, int]


@dataclass(frozen=True)
class SnapshotRecord:
    meta: SnapshotMeta
    view: PortfolioView
    composition: dict[str, float]


def connect(db_path: Path | None = None, *, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    if not read_only:
        path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(path), read_only=read_only)


def ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key VARCHAR PRIMARY KEY,
            value VARCHAR NOT NULL
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
        stored = int(row[0])
        if stored != SCHEMA_VERSION:
            raise RuntimeError(
                f"portfolio.duckdb schema_version={stored}, code expects {SCHEMA_VERSION}. "
                "Bump SCHEMA_VERSION and migrate, or delete the DB to rebuild."
            )

    # observed_at stored as ISO-8601 text (avoids DuckDB timestamptz → pytz dependency).
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            id BIGINT PRIMARY KEY,
            observed_at VARCHAR NOT NULL,
            effective_date DATE,
            source VARCHAR NOT NULL,
            fingerprint_hash VARCHAR NOT NULL,
            counts_json VARCHAR NOT NULL,
            raw_json VARCHAR NOT NULL
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshot_composition (
            snapshot_id BIGINT NOT NULL,
            key VARCHAR NOT NULL,
            book VARCHAR NOT NULL,
            quantity DOUBLE NOT NULL,
            PRIMARY KEY (snapshot_id, key)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshot_holdings (
            snapshot_id BIGINT NOT NULL,
            isin VARCHAR,
            exchange VARCHAR,
            tradingsymbol VARCHAR,
            instrument_token BIGINT,
            product VARCHAR,
            quantity BIGINT,
            t1_quantity BIGINT,
            collateral_quantity BIGINT,
            used_quantity BIGINT,
            opening_quantity BIGINT,
            owned_quantity BIGINT,
            average_price DOUBLE,
            last_price DOUBLE,
            close_price DOUBLE,
            pnl DOUBLE,
            day_change DOUBLE,
            day_change_percentage DOUBLE
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshot_mf_holdings (
            snapshot_id BIGINT NOT NULL,
            isin VARCHAR,
            folio VARCHAR,
            fund VARCHAR,
            tradingsymbol VARCHAR,
            quantity DOUBLE,
            average_price DOUBLE,
            last_price DOUBLE,
            pnl DOUBLE
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshot_positions (
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


def _next_id(con: duckdb.DuckDBPyConnection) -> int:
    row = con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM snapshots").fetchone()
    return int(row[0])


def _raw_to_json(raw: RawPayload) -> str:
    payload = {
        "holdings": raw.holdings,
        "positions": raw.positions,
        "mf_holdings": raw.mf_holdings,
        "profile": raw.profile,
        "fetched_at": raw.fetched_at.isoformat(),
    }
    return json.dumps(payload, default=str)


def _parse_observed_at(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def latest_snapshot(con: duckdb.DuckDBPyConnection) -> SnapshotMeta | None:
    row = con.execute(
        """
        SELECT id, observed_at, effective_date, source, fingerprint_hash, counts_json
        FROM snapshots
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    return SnapshotMeta(
        id=int(row[0]),
        observed_at=_parse_observed_at(row[1]),
        effective_date=row[2],
        source=row[3],
        fingerprint_hash=row[4],
        counts=json.loads(row[5]),
    )


def list_snapshots(con: duckdb.DuckDBPyConnection, limit: int = 50) -> list[SnapshotMeta]:
    rows = con.execute(
        """
        SELECT id, observed_at, effective_date, source, fingerprint_hash, counts_json
        FROM snapshots
        ORDER BY id DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()
    return [
        SnapshotMeta(
            id=int(r[0]),
            observed_at=_parse_observed_at(r[1]),
            effective_date=r[2],
            source=r[3],
            fingerprint_hash=r[4],
            counts=json.loads(r[5]),
        )
        for r in rows
    ]


def get_composition(con: duckdb.DuckDBPyConnection, snapshot_id: int) -> dict[str, float]:
    rows = con.execute(
        "SELECT key, quantity FROM snapshot_composition WHERE snapshot_id = ?",
        [snapshot_id],
    ).fetchall()
    return {str(r[0]): float(r[1]) for r in rows}


def get_snapshot(con: duckdb.DuckDBPyConnection, snapshot_id: int) -> SnapshotRecord:
    row = con.execute(
        """
        SELECT id, observed_at, effective_date, source, fingerprint_hash, counts_json, raw_json
        FROM snapshots WHERE id = ?
        """,
        [snapshot_id],
    ).fetchone()
    if row is None:
        raise KeyError(f"snapshot id {snapshot_id} not found")

    raw_data = json.loads(row[6])
    fetched_at = datetime.fromisoformat(raw_data["fetched_at"])
    raw = RawPayload(
        holdings=raw_data.get("holdings") or [],
        positions=raw_data.get("positions") or {"net": [], "day": []},
        mf_holdings=raw_data.get("mf_holdings") or [],
        profile=raw_data.get("profile") or {},
        fetched_at=fetched_at,
    )
    view = to_view(raw)
    meta = SnapshotMeta(
        id=int(row[0]),
        observed_at=_parse_observed_at(row[1]),
        effective_date=row[2],
        source=row[3],
        fingerprint_hash=row[4],
        counts=json.loads(row[5]),
    )
    return SnapshotRecord(
        meta=meta,
        view=view,
        composition=get_composition(con, snapshot_id),
    )


def _insert_holdings(con: duckdb.DuckDBPyConnection, snapshot_id: int, holdings: tuple[Holding, ...]) -> None:
    for h in holdings:
        con.execute(
            """
            INSERT INTO snapshot_holdings VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            [
                snapshot_id,
                h.isin,
                h.exchange,
                h.tradingsymbol,
                h.instrument_token,
                h.product,
                h.quantity,
                h.t1_quantity,
                h.collateral_quantity,
                h.used_quantity,
                h.opening_quantity,
                h.owned_quantity,
                h.average_price,
                h.last_price,
                h.close_price,
                h.pnl,
                h.day_change,
                h.day_change_percentage,
            ],
        )


def _insert_mf(con: duckdb.DuckDBPyConnection, snapshot_id: int, rows: tuple[MfHolding, ...]) -> None:
    for m in rows:
        con.execute(
            """
            INSERT INTO snapshot_mf_holdings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                snapshot_id,
                m.isin,
                m.folio,
                m.fund,
                m.tradingsymbol,
                m.quantity,
                m.average_price,
                m.last_price,
                m.pnl,
            ],
        )


def _insert_positions(
    con: duckdb.DuckDBPyConnection,
    snapshot_id: int,
    kind: str,
    rows: tuple[Position, ...],
) -> None:
    for p in rows:
        con.execute(
            """
            INSERT INTO snapshot_positions VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            [
                snapshot_id,
                kind,
                p.exchange,
                p.tradingsymbol,
                p.instrument_token,
                p.product,
                p.segment,
                p.quantity,
                p.overnight_quantity,
                p.multiplier,
                p.average_price,
                p.last_price,
                p.close_price,
                p.value,
                p.pnl,
                p.m2m,
                p.realised,
                p.unrealised,
                p.buy_quantity,
                p.buy_price,
                p.buy_value,
                p.sell_quantity,
                p.sell_price,
                p.sell_value,
                p.day_buy_quantity,
                p.day_buy_price,
                p.day_buy_value,
                p.day_sell_quantity,
                p.day_sell_price,
                p.day_sell_value,
            ],
        )


def capture(
    con: duckdb.DuckDBPyConnection,
    view: PortfolioView,
    *,
    source: str,
) -> CaptureResult:
    """Persist a snapshot only when composition fingerprint differs from latest."""
    ensure_schema(con)
    comp = fp.composition(view)
    new_hash = fp.fingerprint_hash(comp)

    latest = latest_snapshot(con)
    if latest is not None and latest.fingerprint_hash == new_hash:
        old_comp = get_composition(con, latest.id)
        return CaptureResult(
            saved=False,
            snapshot_id=latest.id,
            fingerprint_hash=new_hash,
            diff=fp.diff(old_comp, comp),
        )

    old_comp = get_composition(con, latest.id) if latest is not None else {}
    delta = fp.diff(old_comp, comp)

    snapshot_id = _next_id(con)
    con.execute("BEGIN")
    try:
        con.execute(
            """
            INSERT INTO snapshots (
                id, observed_at, effective_date, source, fingerprint_hash, counts_json, raw_json
            ) VALUES (?, ?, NULL, ?, ?, ?, ?)
            """,
            [
                snapshot_id,
                view.fetched_at.isoformat(),
                source,
                new_hash,
                json.dumps(view.counts),
                _raw_to_json(view.raw),
            ],
        )
        for key, qty in comp.items():
            con.execute(
                "INSERT INTO snapshot_composition VALUES (?, ?, ?, ?)",
                [snapshot_id, key, fp.book_for_key(key), qty],
            )
        _insert_holdings(con, snapshot_id, view.holdings)
        _insert_mf(con, snapshot_id, view.mf_holdings)
        _insert_positions(con, snapshot_id, "net", view.positions_net)
        _insert_positions(con, snapshot_id, "day", view.positions_day)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise

    return CaptureResult(
        saved=True,
        snapshot_id=snapshot_id,
        fingerprint_hash=new_hash,
        diff=delta,
    )
