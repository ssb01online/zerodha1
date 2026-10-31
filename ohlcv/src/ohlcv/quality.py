"""Offline day/minute coverage snapshots.

One generate pass reads series parquet (and catalog identity) into a versioned
DuckDB file. ``--days N`` is applied when the snapshot is read, not when it
is written.

A session day is any date that appears in that market's day parquet. A traded
day (day volume > 0) is minute-complete when at least one minute candle exists.
TTQ is the sum of day-bar volume in the window. TTV is the sum of
day-bar volume times close.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Sequence

import duckdb

from kitetools import auth

from . import catalog
from .markets import MARKETS, get_market
from .paths import REPORTS_DIR, ensure_dir, paths_for

SCHEMA_VERSION = 2
_REPORT_ID = re.compile(r"^\d{8}-\d{6}(?:-\d+)?$")

_LEVELS = ("market", "underlying", "expiry", "contract")


@dataclass(frozen=True)
class MarketInputs:
    day_dir: Path
    minute_dir: Path
    catalog_path: Path


@dataclass(frozen=True)
class MarketReportInfo:
    market: str
    as_of: date | None
    day_files: int
    minute_files: int
    contracts: int


@dataclass(frozen=True)
class QualityReport:
    path: Path
    generated_at: str
    markets: tuple[MarketReportInfo, ...]


@dataclass(frozen=True)
class QualityRow:
    market: str
    underlying: str | None
    expiry: date | None
    token: int | None
    tradingsymbol: str | None
    instrument_type: str | None
    strike: float | None
    session_days: int
    day_bars: int
    day_gaps: int
    traded_days: int
    minute_days: int
    ttq: int
    ttv: float
    no_day_data: bool

    @property
    def minute_pct(self) -> float | None:
        """Covered traded days / traded days. None when nothing traded."""
        if self.traded_days <= 0:
            return None
        return 100.0 * self.minute_days / self.traded_days


@dataclass(frozen=True)
class ContractSlice:
    row: QualityRow
    gap_dates: tuple[date, ...]
    traded_without_minute: tuple[date, ...]


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _as_optional_date(value: Any) -> date | None:
    if value is None:
        return None
    return _as_date(value)


def _now_ist(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(auth.IST)
    if now.tzinfo is None:
        return now.replace(tzinfo=auth.IST)
    return now.astimezone(auth.IST)


def _series_files(root: Path) -> list[Path]:
    """yyyymmdd.parquet files under ``root/<underlying>/``."""
    if not root.is_dir():
        return []
    found: list[Path] = []
    for folder in sorted(root.iterdir()):
        if not folder.is_dir() or folder.name.startswith("_"):
            continue
        for path in sorted(folder.glob("*.parquet")):
            stem = path.stem
            if len(stem) == 8 and stem.isdigit():
                found.append(path)
    return found


def _inputs_for(market: str, roots: dict[str, MarketInputs] | None) -> MarketInputs:
    if roots is not None and market in roots:
        return roots[market]
    paths = paths_for(market)
    return MarketInputs(
        day_dir=paths.day_dir,
        minute_dir=paths.minute_dir,
        catalog_path=paths.catalog_path,
    )


def _quote_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def _create_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE meta (
            key VARCHAR PRIMARY KEY,
            value VARCHAR NOT NULL
        )
        """
    )
    con.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
        [str(SCHEMA_VERSION)],
    )
    con.execute(
        """
        CREATE TABLE session_day (
            market VARCHAR NOT NULL,
            trade_date DATE NOT NULL,
            PRIMARY KEY (market, trade_date)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE contract (
            market VARCHAR NOT NULL,
            token BIGINT NOT NULL,
            underlying VARCHAR NOT NULL,
            expiry DATE NOT NULL,
            tradingsymbol VARCHAR NOT NULL,
            instrument_type VARCHAR NOT NULL,
            strike DOUBLE NOT NULL,
            lot_size INTEGER NOT NULL,
            in_catalog BOOLEAN NOT NULL,
            PRIMARY KEY (market, token)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE contract_day (
            market VARCHAR NOT NULL,
            token BIGINT NOT NULL,
            trade_date DATE NOT NULL,
            day_volume BIGINT,
            day_close DOUBLE,
            minute_bars INTEGER NOT NULL,
            PRIMARY KEY (market, token, trade_date)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE raw_day (
            market VARCHAR NOT NULL,
            token BIGINT NOT NULL,
            underlying VARCHAR,
            expiry DATE,
            tradingsymbol VARCHAR,
            instrument_type VARCHAR,
            strike DOUBLE,
            lot_size INTEGER,
            trade_date DATE NOT NULL,
            day_volume BIGINT,
            day_close DOUBLE
        )
        """
    )
    con.execute(
        """
        CREATE TABLE raw_minute (
            market VARCHAR NOT NULL,
            token BIGINT NOT NULL,
            underlying VARCHAR,
            expiry DATE,
            tradingsymbol VARCHAR,
            instrument_type VARCHAR,
            strike DOUBLE,
            lot_size INTEGER,
            trade_date DATE NOT NULL,
            minute_bars INTEGER NOT NULL
        )
        """
    )


def _insert_day_file(con: duckdb.DuckDBPyConnection, market: str, path: Path) -> None:
    con.execute(
        """
        INSERT INTO raw_day
        SELECT
            ? AS market,
            token,
            any_value(underlying),
            any_value(CAST(expiry AS DATE)),
            any_value(tradingsymbol),
            any_value(instrument_type),
            any_value(CAST(strike AS DOUBLE)),
            any_value(CAST(lot_size AS INTEGER)),
            CAST(ts AS DATE) AS trade_date,
            MAX(CAST(volume AS BIGINT)) AS day_volume,
            arg_max(CAST(close AS DOUBLE), ts) AS day_close
        FROM read_parquet(?)
        WHERE token IS NOT NULL AND ts IS NOT NULL
        GROUP BY token, CAST(ts AS DATE)
        """,
        [market, str(path)],
    )


def _insert_minute_file(con: duckdb.DuckDBPyConnection, market: str, path: Path) -> None:
    con.execute(
        """
        INSERT INTO raw_minute
        SELECT
            ? AS market,
            token,
            any_value(underlying),
            any_value(CAST(expiry AS DATE)),
            any_value(tradingsymbol),
            any_value(instrument_type),
            any_value(CAST(strike AS DOUBLE)),
            any_value(CAST(lot_size AS INTEGER)),
            CAST(ts AS DATE) AS trade_date,
            CAST(COUNT(*) AS INTEGER) AS minute_bars
        FROM read_parquet(?)
        WHERE token IS NOT NULL AND ts IS NOT NULL
        GROUP BY token, CAST(ts AS DATE)
        """,
        [market, str(path)],
    )


def _finalize_facts(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        INSERT INTO session_day (market, trade_date)
        SELECT DISTINCT market, trade_date
        FROM raw_day
        WHERE trade_date IS NOT NULL
        """
    )
    con.execute(
        """
        INSERT INTO contract (
            market, token, underlying, expiry, tradingsymbol, instrument_type,
            strike, lot_size, in_catalog
        )
        SELECT
            market,
            token,
            COALESCE(underlying, ''),
            expiry,
            COALESCE(tradingsymbol, ''),
            COALESCE(instrument_type, ''),
            COALESCE(strike, 0),
            COALESCE(lot_size, 0),
            FALSE
        FROM (
            SELECT
                market, token, underlying, expiry, tradingsymbol, instrument_type,
                strike, lot_size,
                ROW_NUMBER() OVER (
                    PARTITION BY market, token
                    ORDER BY src, expiry
                ) AS rn
            FROM (
                SELECT
                    market, token, underlying, expiry, tradingsymbol, instrument_type,
                    strike, lot_size, 0 AS src
                FROM raw_day
                UNION ALL
                SELECT
                    market, token, underlying, expiry, tradingsymbol, instrument_type,
                    strike, lot_size, 1 AS src
                FROM raw_minute
            ) ids
            WHERE expiry IS NOT NULL
        ) picked
        WHERE rn = 1
        """
    )
    con.execute(
        """
        INSERT INTO contract_day (market, token, trade_date, day_volume, day_close, minute_bars)
        SELECT
            COALESCE(d.market, m.market),
            COALESCE(d.token, m.token),
            COALESCE(d.trade_date, m.trade_date),
            d.day_volume,
            d.day_close,
            COALESCE(m.minute_bars, 0)
        FROM (
            SELECT
                market,
                token,
                trade_date,
                MAX(day_volume) AS day_volume,
                arg_max(day_close, COALESCE(day_volume, -1)) AS day_close
            FROM raw_day
            GROUP BY market, token, trade_date
        ) d
        FULL OUTER JOIN (
            SELECT market, token, trade_date, SUM(minute_bars)::INTEGER AS minute_bars
            FROM raw_minute
            GROUP BY market, token, trade_date
        ) m
        USING (market, token, trade_date)
        """
    )
    con.execute("DROP TABLE raw_day")
    con.execute("DROP TABLE raw_minute")


def _merge_catalog(con: duckdb.DuckDBPyConnection, market: str, catalog_path: Path) -> None:
    """Copy contract identity from a market catalog. Missing catalogs are skipped."""
    if not catalog_path.is_file():
        return
    # Open read-only first so a writer lock surfaces as CatalogLocked.
    probe = catalog.connect(catalog_path, read_only=True)
    probe.close()
    con.execute(f"ATTACH '{_quote_path(catalog_path)}' AS cat (READ_ONLY)")
    try:
        con.execute(
            """
            UPDATE contract
            SET in_catalog = TRUE
            WHERE market = ?
              AND token IN (SELECT token FROM cat.contracts)
            """,
            [market],
        )
        con.execute(
            """
            INSERT INTO contract (
                market, token, underlying, expiry, tradingsymbol, instrument_type,
                strike, lot_size, in_catalog
            )
            SELECT
                ?,
                c.token,
                c.underlying,
                c.expiry,
                c.tradingsymbol,
                c.instrument_type,
                c.strike,
                c.lot_size,
                TRUE
            FROM cat.contracts c
            WHERE NOT EXISTS (
                SELECT 1 FROM contract q
                WHERE q.market = ? AND q.token = c.token
            )
            """,
            [market, market],
        )
    finally:
        con.execute("DETACH cat")


def _report_dir(root: Path, now: datetime) -> Path:
    stamp = now.strftime("%Y%m%d-%H%M%S")
    path = root / stamp
    if not path.exists():
        return path
    suffix = 2
    while True:
        candidate = root / f"{stamp}-{suffix}"
        if not candidate.exists():
            return candidate
        suffix += 1


def generate(
    markets: Sequence[str] | None = None,
    *,
    reports_root: Path | None = None,
    now: datetime | None = None,
    roots: dict[str, MarketInputs] | None = None,
) -> QualityReport:
    """Scan series files into a new ``_reports/<id>/`` snapshot. Does not call Kite."""
    chosen = [get_market(m).id for m in (markets if markets is not None else list(MARKETS))]
    moment = _now_ist(now)
    root = ensure_dir(reports_root if reports_root is not None else REPORTS_DIR)
    report_dir = ensure_dir(_report_dir(root, moment))
    db_path = report_dir / "quality.duckdb"

    con = duckdb.connect(str(db_path))
    file_counts: dict[str, tuple[int, int]] = {}
    try:
        con.execute("SET memory_limit='4GB'")
        _create_schema(con)
        for market in chosen:
            inputs = _inputs_for(market, roots)
            day_files = _series_files(inputs.day_dir)
            minute_files = _series_files(inputs.minute_dir)
            file_counts[market] = (len(day_files), len(minute_files))
            for path in day_files:
                _insert_day_file(con, market, path)
            for path in minute_files:
                _insert_minute_file(con, market, path)
        _finalize_facts(con)
        for market in chosen:
            _merge_catalog(con, market, _inputs_for(market, roots).catalog_path)
    finally:
        con.close()

    infos: list[MarketReportInfo] = []
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        for market in chosen:
            as_of_row = con.execute(
                "SELECT MAX(trade_date) FROM session_day WHERE market = ?",
                [market],
            ).fetchone()
            count_row = con.execute(
                "SELECT COUNT(*) FROM contract WHERE market = ?",
                [market],
            ).fetchone()
            day_files, minute_files = file_counts[market]
            infos.append(
                MarketReportInfo(
                    market=market,
                    as_of=_as_optional_date(as_of_row[0]) if as_of_row else None,
                    day_files=day_files,
                    minute_files=minute_files,
                    contracts=int(count_row[0]) if count_row else 0,
                )
            )
    finally:
        con.close()

    generated_at = moment.isoformat(timespec="seconds")
    manifest = {
        "generated_at": generated_at,
        "schema_version": SCHEMA_VERSION,
        "markets": {
            info.market: {
                "as_of": info.as_of.isoformat() if info.as_of is not None else None,
                "day_files": info.day_files,
                "minute_files": info.minute_files,
                "contracts": info.contracts,
            }
            for info in infos
        },
    }
    (report_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    return QualityReport(path=report_dir, generated_at=generated_at, markets=tuple(infos))


def load_manifest(report_dir: Path) -> dict[str, Any]:
    path = report_dir / "manifest.json"
    if not path.is_file():
        raise FileNotFoundError(f"quality manifest not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def list_reports(reports_root: Path | None = None) -> list[Path]:
    root = reports_root if reports_root is not None else REPORTS_DIR
    if not root.is_dir():
        return []
    found = [
        path
        for path in root.iterdir()
        if path.is_dir() and _REPORT_ID.match(path.name) and (path / "manifest.json").is_file()
    ]
    return sorted(found, key=lambda path: path.name)


def resolve_report(report_id: str | None = None, *, reports_root: Path | None = None) -> Path:
    """Latest snapshot, or the directory named ``report_id`` (id or path)."""
    root = reports_root if reports_root is not None else REPORTS_DIR
    if report_id:
        direct = Path(report_id)
        if direct.is_dir() and (direct / "manifest.json").is_file():
            return direct
        named = root / report_id
        if named.is_dir() and (named / "manifest.json").is_file():
            return named
        raise FileNotFoundError(
            f"quality report not found: {report_id}. Run: uv run ohlcv quality"
        )
    reports = list_reports(root)
    if not reports:
        raise FileNotFoundError("no quality report found. Run: uv run ohlcv quality")
    return reports[-1]


def connect_report(report_dir: Path, *, read_only: bool = True) -> duckdb.DuckDBPyConnection:
    db_path = report_dir / "quality.duckdb"
    if not db_path.is_file():
        raise FileNotFoundError(f"quality database not found: {db_path}")
    return duckdb.connect(str(db_path), read_only=read_only)


def _check_days(days: int | None) -> None:
    if days is not None and days < 1:
        raise ValueError(f"--days must be >= 1, got {days}")


def _check_level(level: str) -> str:
    key = (level or "").strip().lower()
    if key not in _LEVELS:
        raise ValueError(f"unknown level {level!r}; expected one of: {', '.join(_LEVELS)}")
    return key


def _filter_sql(
    *,
    market: str | None,
    underlying: str | None,
    expiry: date | None,
    token: int | None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if market is not None:
        clauses.append("c.market = ?")
        params.append(market)
    if underlying is not None:
        clauses.append("c.underlying = ?")
        params.append(underlying)
    if expiry is not None:
        clauses.append("c.expiry = ?")
        params.append(expiry)
    if token is not None:
        clauses.append("c.token = ?")
        params.append(token)
    if not clauses:
        return "", params
    return " WHERE " + " AND ".join(clauses), params


_METRICS_BODY = """
ranked AS (
    SELECT
        market,
        trade_date,
        ROW_NUMBER() OVER (PARTITION BY market ORDER BY trade_date DESC) AS rn
    FROM session_day
),
window_days AS (
    SELECT market, trade_date
    FROM ranked
    WHERE (? IS NULL OR rn <= ?)
),
first_bar AS (
    SELECT market, token, MIN(trade_date) AS first_day
    FROM contract_day
    WHERE day_volume IS NOT NULL
    GROUP BY market, token
),
metrics AS (
    SELECT
        c.market,
        c.underlying,
        c.expiry,
        c.token,
        c.tradingsymbol,
        c.instrument_type,
        c.strike,
        CASE WHEN fb.first_day IS NULL THEN 1 ELSE 0 END AS no_day_data,
        COALESCE(SUM(CASE WHEN w.trade_date IS NOT NULL THEN 1 ELSE 0 END), 0) AS session_days,
        COALESCE(
            SUM(CASE WHEN w.trade_date IS NOT NULL AND cd.day_volume IS NOT NULL THEN 1 ELSE 0 END),
            0
        ) AS day_bars,
        COALESCE(
            SUM(CASE WHEN w.trade_date IS NOT NULL AND cd.day_volume IS NULL THEN 1 ELSE 0 END),
            0
        ) AS day_gaps,
        COALESCE(SUM(CASE WHEN cd.day_volume > 0 THEN 1 ELSE 0 END), 0) AS traded_days,
        COALESCE(
            SUM(
                CASE
                    WHEN cd.day_volume > 0 AND COALESCE(cd.minute_bars, 0) >= 1 THEN 1
                    ELSE 0
                END
            ),
            0
        ) AS minute_days,
        COALESCE(
            SUM(CASE WHEN cd.day_volume IS NOT NULL THEN cd.day_volume ELSE 0 END),
            0
        ) AS ttq,
        COALESCE(
            SUM(
                CASE
                    WHEN cd.day_volume IS NOT NULL
                    THEN cd.day_volume * COALESCE(cd.day_close, 0)
                    ELSE 0
                END
            ),
            0
        ) AS ttv
    FROM contract c
    LEFT JOIN first_bar fb
        ON fb.market = c.market AND fb.token = c.token
    LEFT JOIN window_days w
        ON w.market = c.market
       AND fb.first_day IS NOT NULL
       AND w.trade_date >= fb.first_day
       AND w.trade_date <= c.expiry
    LEFT JOIN contract_day cd
        ON cd.market = c.market
       AND cd.token = c.token
       AND cd.trade_date = w.trade_date
    {where}
    GROUP BY
        c.market, c.underlying, c.expiry, c.token, c.tradingsymbol,
        c.instrument_type, c.strike, fb.first_day
)
"""


def _metrics_sql(where: str) -> str:
    return _METRICS_BODY.format(where=where)


def _row_from_contract(record: tuple[Any, ...]) -> QualityRow:
    return QualityRow(
        market=str(record[0]),
        underlying=str(record[1]) if record[1] is not None else None,
        expiry=_as_optional_date(record[2]),
        token=int(record[3]) if record[3] is not None else None,
        tradingsymbol=str(record[4]) if record[4] is not None else None,
        instrument_type=str(record[5]) if record[5] is not None else None,
        strike=float(record[6]) if record[6] is not None else None,
        session_days=int(record[7] or 0),
        day_bars=int(record[8] or 0),
        day_gaps=int(record[9] or 0),
        traded_days=int(record[10] or 0),
        minute_days=int(record[11] or 0),
        ttq=int(record[12] or 0),
        ttv=float(record[13] or 0),
        no_day_data=bool(record[14]),
    )


def _row_from_rollup(record: tuple[Any, ...], *, level: str) -> QualityRow:
    market = str(record[0])
    underlying = str(record[1]) if level in ("underlying", "expiry") and record[1] is not None else None
    expiry = _as_optional_date(record[2]) if level == "expiry" else None
    return QualityRow(
        market=market,
        underlying=underlying,
        expiry=expiry,
        token=None,
        tradingsymbol=None,
        instrument_type=None,
        strike=None,
        session_days=int(record[3] or 0),
        day_bars=int(record[4] or 0),
        day_gaps=int(record[5] or 0),
        traded_days=int(record[6] or 0),
        minute_days=int(record[7] or 0),
        ttq=int(record[8] or 0),
        ttv=float(record[9] or 0),
        no_day_data=False,
    )


def summarize(
    con: duckdb.DuckDBPyConnection,
    *,
    days: int | None = None,
    market: str | None = None,
    underlying: str | None = None,
    expiry: date | None = None,
    level: str = "market",
    include_markets: Sequence[str] | None = None,
) -> list[QualityRow]:
    """Aggregate one snapshot for the last ``days`` session days (None = all)."""
    _check_days(days)
    level = _check_level(level)
    where, filter_params = _filter_sql(
        market=market, underlying=underlying, expiry=expiry, token=None
    )
    params: list[Any] = [days, days, *filter_params]
    cte = _metrics_sql(where)
    if level == "contract":
        sql = f"""
            WITH {cte}
            SELECT
                market, underlying, expiry, token, tradingsymbol, instrument_type, strike,
                session_days, day_bars, day_gaps, traded_days, minute_days, ttq, ttv, no_day_data
            FROM metrics
            ORDER BY market, underlying, expiry, instrument_type, strike, token
        """
        rows = [_row_from_contract(record) for record in con.execute(sql, params).fetchall()]
        return rows

    if level == "market":
        select = """
            market,
            CAST(NULL AS VARCHAR) AS underlying,
            CAST(NULL AS DATE) AS expiry,
        """
        group = "market"
        order = "market"
    elif level == "underlying":
        select = """
            market,
            underlying,
            CAST(NULL AS DATE) AS expiry,
        """
        group = "market, underlying"
        order = "market, underlying"
    else:
        select = """
            market,
            underlying,
            expiry,
        """
        group = "market, underlying, expiry"
        order = "market, underlying, expiry"
    sql = f"""
        WITH {cte}
        SELECT
            {select}
            SUM(session_days),
            SUM(day_bars),
            SUM(day_gaps),
            SUM(traded_days),
            SUM(minute_days),
            SUM(ttq),
            SUM(ttv)
        FROM metrics
        GROUP BY {group}
        ORDER BY {order}
    """
    rows = [_row_from_rollup(record, level=level) for record in con.execute(sql, params).fetchall()]
    if level == "market" and include_markets:
        present = {row.market for row in rows}
        for market_id in include_markets:
            if market is not None and market_id != market:
                continue
            if market_id in present:
                continue
            rows.append(
                QualityRow(
                    market=market_id,
                    underlying=None,
                    expiry=None,
                    token=None,
                    tradingsymbol=None,
                    instrument_type=None,
                    strike=None,
                    session_days=0,
                    day_bars=0,
                    day_gaps=0,
                    traded_days=0,
                    minute_days=0,
                    ttq=0,
                    ttv=0.0,
                    no_day_data=False,
                )
            )
        rows.sort(key=lambda row: row.market)
    return rows


def contract_slices(
    con: duckdb.DuckDBPyConnection,
    *,
    days: int | None = None,
    market: str | None = None,
    underlying: str | None = None,
    expiry: date | None = None,
    token: int | None = None,
) -> list[ContractSlice]:
    """Per-contract totals plus gap dates and traded dates that have no minute bar."""
    _check_days(days)
    where, filter_params = _filter_sql(
        market=market, underlying=underlying, expiry=expiry, token=token
    )
    params: list[Any] = [days, days, *filter_params]
    totals_sql = f"""
        WITH {_metrics_sql(where)}
        SELECT
            market, underlying, expiry, token, tradingsymbol, instrument_type, strike,
            session_days, day_bars, day_gaps, traded_days, minute_days, ttq, ttv, no_day_data
        FROM metrics
        ORDER BY market, underlying, expiry, token
    """
    total_rows = [_row_from_contract(record) for record in con.execute(totals_sql, params).fetchall()]

    dates_sql = f"""
        WITH ranked AS (
            SELECT
                market,
                trade_date,
                ROW_NUMBER() OVER (PARTITION BY market ORDER BY trade_date DESC) AS rn
            FROM session_day
        ),
        window_days AS (
            SELECT market, trade_date
            FROM ranked
            WHERE (? IS NULL OR rn <= ?)
        ),
        first_bar AS (
            SELECT market, token, MIN(trade_date) AS first_day
            FROM contract_day
            WHERE day_volume IS NOT NULL
            GROUP BY market, token
        )
        SELECT
            c.market,
            c.token,
            w.trade_date,
            cd.day_volume,
            cd.minute_bars
        FROM contract c
        JOIN first_bar fb ON fb.market = c.market AND fb.token = c.token
        JOIN window_days w
            ON w.market = c.market
           AND w.trade_date >= fb.first_day
           AND w.trade_date <= c.expiry
        LEFT JOIN contract_day cd
            ON cd.market = c.market
           AND cd.token = c.token
           AND cd.trade_date = w.trade_date
        {where}
        ORDER BY c.market, c.token, w.trade_date
    """
    gaps: dict[tuple[str, int], list[date]] = {}
    missing_minute: dict[tuple[str, int], list[date]] = {}
    for market_id, token_id, trade_date, day_volume, minute_bars in con.execute(dates_sql, params).fetchall():
        key = (str(market_id), int(token_id))
        day = _as_date(trade_date)
        if day_volume is None:
            gaps.setdefault(key, []).append(day)
        elif int(day_volume) > 0 and int(minute_bars or 0) < 1:
            missing_minute.setdefault(key, []).append(day)

    slices: list[ContractSlice] = []
    for row in total_rows:
        key = (row.market, int(row.token or 0))
        slices.append(
            ContractSlice(
                row=row,
                gap_dates=tuple(gaps.get(key, ())),
                traded_without_minute=tuple(missing_minute.get(key, ())),
            )
        )
    return slices
