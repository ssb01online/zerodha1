"""End-of-day orchestrator: wait for market ready times, then daily + fetch.

``session_end`` on MarketProfile stays the watermark / final-snapshot gate.
``download_ready`` adds a buffer (default 30m) before the orchestrator starts
network download for that market.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, time as dt_time
from pathlib import Path
from typing import Callable, Sequence

import duckdb

from kitetools import auth

from . import catalog
from . import fetch as fetch_mod
from . import planner
from . import rank as rank_mod
from . import snapshot
from . import universe
from .catalog import ContractRow
from .client import KiteClient, RetryExhausted, SessionExpired
from .markets import MARKETS, MarketProfile, get_market
from .paths import EOD_DIR, ensure_dir, paths_for, ymd
from .universe import Series

DEFAULT_READY_BUFFER = timedelta(minutes=30)
DEFAULT_MARKET_ORDER: tuple[str, ...] = ("nfo", "cds", "mcx")
# Align with kitetools.auth.TOKEN_RESET — before 06:00 IST, trade_date is yesterday.
_TRADE_DATE_CUTOFF = dt_time(6, 0)
HEARTBEAT_S = 300.0
CATCHUP_LOCK_PATH = EOD_DIR / "catchup.lock"


class CatchupInProgress(Exception):
    """Another ``ohlcv eod --catch-up`` process holds the catch-up lock."""


@dataclass(frozen=True)
class MarketPlan:
    market_id: str
    exchange: str
    session_end: dt_time
    ready_at: datetime
    wait_seconds: float
    expiry_today: int
    unfinished: int


@dataclass(frozen=True)
class EodPlan:
    trade_date: date
    markets: tuple[MarketPlan, ...]
    buffer: timedelta


class EodSkipped(Exception):
    """Market skipped (not ready under --no-wait)."""


class EodNonSession(Exception):
    """Daily snapshot rejected (weekend / holiday / no volume)."""


def resolve_trade_date(now: datetime | None = None) -> date:
    """IST calendar trade date for EOD jobs.

    Before 06:00 IST, return yesterday (MCX task at 00:05 still targets the
    session that closed the previous evening).
    """
    now = _aware_ist(now)
    if now.time() < _TRADE_DATE_CUTOFF:
        return now.date() - timedelta(days=1)
    return now.date()


def download_ready(
    trade_date: date,
    profile: MarketProfile | str,
    *,
    buffer: timedelta = DEFAULT_READY_BUFFER,
) -> datetime:
    """IST instant when historical download may start for ``trade_date``."""
    if not isinstance(profile, MarketProfile):
        profile = get_market(profile)
    end = datetime.combine(trade_date, profile.session_end, tzinfo=auth.IST)
    return end + buffer


def done_marker_path(market_id: str, trade_date: date, *, eod_dir: Path | None = None) -> Path:
    """Success marker: ``data/ohlcv/_eod/{market}-{yyyymmdd}.done``."""
    root = eod_dir if eod_dir is not None else EOD_DIR
    return root / f"{market_id.lower()}-{ymd(trade_date)}.done"


def is_eod_done(
    market_id: str,
    trade_date: date,
    *,
    eod_dir: Path | None = None,
) -> bool:
    return done_marker_path(market_id, trade_date, eod_dir=eod_dir).is_file()


def mark_eod_done(
    market_id: str,
    trade_date: date,
    *,
    eod_dir: Path | None = None,
    now: datetime | None = None,
) -> Path:
    """Write success marker after a completed per-market EOD run."""
    path = done_marker_path(market_id, trade_date, eod_dir=eod_dir)
    ensure_dir(path.parent)
    stamp = _aware_ist(now).strftime("%Y-%m-%d %H:%M:%S %Z")
    path.write_text(f"done {stamp}\n", encoding="utf-8")
    return path


def missed_markets(
    *,
    now: datetime | None = None,
    trade_date: date | None = None,
    markets: Sequence[str] | None = None,
    buffer: timedelta = DEFAULT_READY_BUFFER,
    eod_dir: Path | None = None,
) -> list[str]:
    """Markets past ``download_ready`` that lack a success ``.done`` marker."""
    now = _aware_ist(now)
    trade_date = trade_date if trade_date is not None else resolve_trade_date(now)
    market_ids = list(markets) if markets is not None else list(DEFAULT_MARKET_ORDER)
    out: list[str] = []
    for mid in market_ids:
        ready = download_ready(trade_date, mid, buffer=buffer)
        if now >= ready and not is_eod_done(mid, trade_date, eod_dir=eod_dir):
            out.append(mid)
    return out


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def catchup_lock_held(*, lock_path: Path | None = None) -> bool:
    """True if another live process owns the catch-up PID lock."""
    path = lock_path if lock_path is not None else CATCHUP_LOCK_PATH
    if not path.exists():
        return False
    try:
        old = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    if old == os.getpid():
        return False
    return _pid_alive(old)


def try_acquire_catchup_lock(*, lock_path: Path | None = None) -> bool:
    """Return True if this process owns the catch-up lock."""
    path = lock_path if lock_path is not None else CATCHUP_LOCK_PATH
    ensure_dir(path.parent)
    if catchup_lock_held(lock_path=path):
        return False
    path.write_text(str(os.getpid()), encoding="utf-8")
    return True


def release_catchup_lock(*, lock_path: Path | None = None) -> None:
    path = lock_path if lock_path is not None else CATCHUP_LOCK_PATH
    try:
        if path.exists():
            text = path.read_text(encoding="utf-8").strip()
            if text == str(os.getpid()):
                path.unlink(missing_ok=True)
    except OSError:
        pass


def parse_markets_arg(text: str | Sequence[str] | None) -> list[str]:
    """Parse ``nfo,cds,mcx`` preserving DEFAULT_MARKET_ORDER among selected."""
    if text is None or text == "":
        return list(DEFAULT_MARKET_ORDER)
    if isinstance(text, str):
        parts = [p.strip().lower() for p in text.replace(";", ",").split(",") if p.strip()]
    else:
        parts = [str(p).strip().lower() for p in text if str(p).strip()]
    if not parts:
        return list(DEFAULT_MARKET_ORDER)
    unknown = [p for p in parts if p not in MARKETS]
    if unknown:
        known = ", ".join(sorted(MARKETS))
        raise ValueError(f"unknown market(s) {unknown}; expected one of: {known}")
    selected = set(parts)
    return [m for m in DEFAULT_MARKET_ORDER if m in selected]


def parse_buffer_arg(text: str | timedelta | None) -> timedelta:
    if text is None:
        return DEFAULT_READY_BUFFER
    if isinstance(text, timedelta):
        return text
    raw = str(text).strip().lower()
    if raw.endswith("m"):
        return timedelta(minutes=int(raw[:-1]))
    if raw.endswith("h"):
        return timedelta(hours=int(raw[:-1]))
    if raw.endswith("s"):
        return timedelta(seconds=int(raw[:-1]))
    return timedelta(minutes=int(raw))


def _aware_ist(now: datetime | None) -> datetime:
    now = now if now is not None else datetime.now(auth.IST)
    if now.tzinfo is None:
        return now.replace(tzinfo=auth.IST)
    return now.astimezone(auth.IST)


def wait_until(
    target: datetime,
    *,
    now: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
    on_heartbeat: Callable[[float, datetime], None] | None = None,
    heartbeat_s: float = HEARTBEAT_S,
    should_stop: Callable[[], str | None] | None = None,
) -> datetime:
    """Block until ``target`` IST (return immediately if already past)."""
    now = _aware_ist(now)
    if target.tzinfo is None:
        target = target.replace(tzinfo=auth.IST)
    else:
        target = target.astimezone(auth.IST)
    remaining = (target - now).total_seconds()
    if remaining <= 0:
        return now
    end = time.monotonic() + remaining
    next_hb = time.monotonic()
    if on_heartbeat is not None:
        on_heartbeat(remaining, target)
        next_hb = time.monotonic() + heartbeat_s
    while True:
        if should_stop is not None:
            reason = should_stop()
            if reason:
                raise KeyboardInterrupt(reason)
        left = end - time.monotonic()
        if left <= 0:
            break
        if on_heartbeat is not None and time.monotonic() >= next_hb:
            on_heartbeat(left, target)
            next_hb = time.monotonic() + heartbeat_s
        sleep(min(left, 1.0))
    return _aware_ist(None)


def snapshot_contracts_for_trade_date(
    con: duckdb.DuckDBPyConnection,
    trade_date: date,
    *,
    calendar_today: date | None = None,
) -> list[ContractRow]:
    """Contracts visible for a trade_date snapshot (incl. post-midnight MCX).

    After 00:00 IST, still-live rows have ``last_seen == calendar_today`` while
    expired-that-day rows keep ``last_seen == trade_date``.
    """
    today = calendar_today if calendar_today is not None else datetime.now(auth.IST).date()
    allowed = {trade_date, today}
    return [c for c in catalog.list_contracts(con) if c.last_seen in allowed]


def select_eod_series(
    con: duckdb.DuckDBPyConnection,
    trade_date: date,
    *,
    interval: str = "minute",
    market: MarketProfile | str | None = None,
    types: universe.TypesFilterName | frozenset[str] | None = None,
) -> list[Series]:
    """Expiry-on-trade_date series plus unfinished older series (catalog scan)."""
    profile = universe.resolve_market(market)
    type_filter = universe.resolve_types_filter(types)
    all_contracts = catalog.list_contracts(con)
    if type_filter is not None:
        all_contracts = [c for c in all_contracts if c.instrument_type in type_filter]
    grouped = universe._group_by_series(all_contracts)
    tokens = [c.token for c in all_contracts]
    fetch_states = catalog.list_fetch_states(con, interval=interval, tokens=tokens)

    out: list[Series] = []
    for (underlying, expiry), contracts in grouped.items():
        if expiry == trade_date:
            out.append(
                Series(
                    underlying=underlying,
                    expiry=expiry,
                    contracts=tuple(sorted(contracts, key=lambda c: c.token)),
                )
            )
            continue
        if expiry < trade_date and universe._series_fetch_incomplete(
            contracts, fetch_states, expiry, profile.session_end
        ):
            out.append(
                Series(
                    underlying=underlying,
                    expiry=expiry,
                    contracts=tuple(sorted(contracts, key=lambda c: c.token)),
                )
            )
    return out


def count_eod_buckets(
    con: duckdb.DuckDBPyConnection,
    trade_date: date,
    *,
    market: MarketProfile | str | None = None,
    types: universe.TypesFilterName | frozenset[str] | None = None,
) -> tuple[int, int]:
    """Return (expiry_today_series, unfinished_series) counts."""
    series = select_eod_series(con, trade_date, interval="minute", market=market, types=types)
    expiry_today = sum(1 for s in series if s.expiry == trade_date)
    unfinished = sum(1 for s in series if s.expiry < trade_date)
    return expiry_today, unfinished


def build_plan(
    *,
    trade_date: date | None = None,
    markets: Sequence[str] | None = None,
    buffer: timedelta = DEFAULT_READY_BUFFER,
    now: datetime | None = None,
    catalog_counts: dict[str, tuple[int, int]] | None = None,
) -> EodPlan:
    now = _aware_ist(now)
    trade_date = trade_date if trade_date is not None else resolve_trade_date(now)
    market_ids = list(markets) if markets is not None else list(DEFAULT_MARKET_ORDER)
    plans: list[MarketPlan] = []
    for mid in market_ids:
        profile = get_market(mid)
        ready = download_ready(trade_date, profile, buffer=buffer)
        wait = max(0.0, (ready - now).total_seconds())
        exp_n, unf_n = (0, 0)
        if catalog_counts and mid in catalog_counts:
            exp_n, unf_n = catalog_counts[mid]
        plans.append(
            MarketPlan(
                market_id=mid,
                exchange=profile.exchange,
                session_end=profile.session_end,
                ready_at=ready,
                wait_seconds=wait,
                expiry_today=exp_n,
                unfinished=unf_n,
            )
        )
    return EodPlan(trade_date=trade_date, markets=tuple(plans), buffer=buffer)


def format_plan(plan: EodPlan) -> str:
    lines = [
        f"trade_date={ymd(plan.trade_date)} IST  buffer={int(plan.buffer.total_seconds() // 60)}m",
        f"{'market':<6} {'session_end':>11} {'ready':>16} {'wait':>10} "
        f"{'exp_today':>9} {'unfinished':>10}",
        "-" * 72,
    ]
    for m in plan.markets:
        wait = _format_wait(m.wait_seconds)
        ready = m.ready_at.strftime("%Y-%m-%d %H:%M")
        lines.append(
            f"{m.market_id:<6} {m.session_end.strftime('%H:%M'):>11} {ready:>16} "
            f"{wait:>10} {m.expiry_today:>9} {m.unfinished:>10}"
        )
    order = " → ".join(m.market_id for m in plan.markets)
    lines.append(f"order: {order}")
    return "\n".join(lines)


def _format_wait(seconds: float) -> str:
    if seconds <= 0:
        return "ready"
    secs = int(seconds)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def ensure_token() -> None:
    """Raise auth.NotLoggedIn if session missing/expired."""
    auth.load_token()


def run_market_eod(
    market_id: str,
    *,
    trade_date: date,
    buffer: timedelta = DEFAULT_READY_BUFFER,
    now: datetime | None = None,
    no_wait: bool = False,
    force: bool = False,
    dry_run: bool = False,
    max_series: int | None = None,
    backfill_days: int | None = planner.BACKFILL_WINDOW_DAYS,
    types: str = "all",
    sleep: Callable[[float], None] = time.sleep,
    on_heartbeat: Callable[[float, datetime], None] | None = None,
    should_stop: Callable[[], str | None] | None = None,
    interrupt: object | None = None,
    client: KiteClient | None = None,
    fetch_cmd: str = "ohlcv eod",
    fetch_lock_path: Path | None = None,
) -> dict:
    """Run sync → daily → fetch for one market. Returns a summary dict."""
    from .progress import InterruptController

    profile = get_market(market_id)
    mpaths = paths_for(market_id)
    now = _aware_ist(now)
    ready = download_ready(trade_date, profile, buffer=buffer)
    own_ctrl = not isinstance(interrupt, InterruptController)
    ctrl: InterruptController = (
        interrupt if isinstance(interrupt, InterruptController) else InterruptController()
    )
    if own_ctrl and not dry_run:
        ctrl.install()

    def _stop() -> str | None:
        if should_stop is not None:
            reason = should_stop()
            if reason:
                return reason
        return ctrl.should_stop()

    try:
        return _run_market_eod_body(
            market_id,
            profile=profile,
            mpaths=mpaths,
            trade_date=trade_date,
            buffer=buffer,
            now=now,
            ready=ready,
            no_wait=no_wait,
            force=force,
            dry_run=dry_run,
            max_series=max_series,
            backfill_days=backfill_days,
            types=types,
            sleep=sleep,
            on_heartbeat=on_heartbeat,
            client=client,
            ctrl=ctrl,
            stop=_stop,
            fetch_cmd=fetch_cmd,
            fetch_lock_path=fetch_lock_path,
        )
    finally:
        if own_ctrl:
            ctrl.restore()


def _run_market_eod_body(
    market_id: str,
    *,
    profile: MarketProfile,
    mpaths,
    trade_date: date,
    buffer: timedelta,
    now: datetime,
    ready: datetime,
    no_wait: bool,
    force: bool,
    dry_run: bool,
    max_series: int | None,
    backfill_days: int | None,
    types: str,
    sleep: Callable[[float], None],
    on_heartbeat: Callable[[float, datetime], None] | None,
    client: KiteClient | None,
    ctrl,
    stop: Callable[[], str | None],
    fetch_cmd: str = "ohlcv eod",
    fetch_lock_path: Path | None = None,
) -> dict:
    # --no-wait skips not-yet-ready markets (including dry-run). dry-run never sleeps.
    # --force proceeds early (may be intraday).
    if not force and no_wait and now < ready:
        raise EodSkipped(
            f"{market_id} not ready until {ready.strftime('%Y-%m-%d %H:%M')} IST "
            f"({_format_wait((ready - now).total_seconds())} left); use wait mode or --force"
        )
    if not dry_run and not force and not no_wait and now < ready:
        def _hb(left: float, target: datetime) -> None:
            if on_heartbeat is not None:
                on_heartbeat(left, target)
            else:
                print(
                    f"  waiting for {market_id} ready {target.strftime('%H:%M')} IST "
                    f"({_format_wait(left)} left)"
                )

        now = wait_until(
            ready,
            now=now,
            sleep=sleep,
            on_heartbeat=_hb,
            should_stop=stop,
        )

    if dry_run:
        if not mpaths.catalog_path.exists():
            return {
                "market": market_id,
                "trade_date": trade_date,
                "dry_run": True,
                "ready_at": ready,
                "expiry_today": 0,
                "unfinished": 0,
                "series": 0,
            }
        con = catalog.connect(mpaths.catalog_path)
        try:
            catalog.ensure_schema(con)
            series = select_eod_series(
                con, trade_date, interval="minute", market=market_id, types=types
            )
            if max_series is not None:
                series = series[:max_series]
            exp_n = sum(1 for s in series if s.expiry == trade_date)
            unf_n = sum(1 for s in series if s.expiry < trade_date)
            return {
                "market": market_id,
                "trade_date": trade_date,
                "dry_run": True,
                "ready_at": ready,
                "expiry_today": exp_n,
                "unfinished": unf_n,
                "series": len(series),
            }
        finally:
            con.close()

    from . import fetch_lock

    fetch_lock.try_acquire(
        market_id,
        cmd=f"{fetch_cmd} --markets {market_id}",
        lock_path=fetch_lock_path,
    )
    try:
        ensure_token()
        con = catalog.connect(mpaths.catalog_path)
        try:
            catalog.ensure_schema(con)
            from . import archive as archive_mod
            from instruments import ensure_cache

            ensure_cache()
            archive_mod.ensure_archive()
            allowed = universe.resolve_types_filter(types)
            count = universe.sync_contracts(con, market=market_id, allowed_types=allowed)
            print(f"  synced {count} {profile.exchange} contracts")

            if client is None:
                client = KiteClient(auth.get_kite())

            calendar_today = datetime.now(auth.IST).date()
            contracts = snapshot_contracts_for_trade_date(
                con, trade_date, calendar_today=calendar_today
            )
            try:
                result = snapshot.take_snapshot(
                    client,
                    contracts,
                    market=market_id,
                    now=datetime.now(auth.IST),
                    run_date=trade_date,
                    dest_dir=mpaths.daily_dir,
                    staging_dir=mpaths.staging_dir,
                    should_stop=stop,
                )
            except snapshot.SnapshotRejected as exc:
                raise EodNonSession(str(exc)) from exc

            catalog.record_snapshot(
                con,
                catalog.DailySnapshotMeta(
                    trade_date=result.trade_date,
                    status=result.status,
                    taken_at=result.taken_at,
                    contracts=result.contracts,
                    traded_contracts=result.traded_contracts,
                    missing=result.missing,
                    total_volume=result.total_volume,
                    path=str(result.path),
                ),
            )
            rank_mod.rank_from_snapshot(
                con,
                result.trade_date,
                market=market_id,
                dest_dir=mpaths.daily_dir,
            )
            print(
                f"  daily {ymd(result.trade_date)} status={result.status} "
                f"traded={result.traded_contracts}/{result.contracts}"
            )

            series_day = select_eod_series(
                con, trade_date, interval="day", market=market_id, types=types
            )
            series_min = select_eod_series(
                con, trade_date, interval="minute", market=market_id, types=types
            )
            by_key: dict[tuple[str, date], Series] = {}
            for s in series_day + series_min:
                prev = by_key.get(s.key)
                if prev is None or len(s.contracts) > len(prev.contracts):
                    by_key[s.key] = s
            series_list = list(by_key.values())
            ranks = rank_mod.load_ranks(con)
            tokens = [c.token for s in series_list for c in s.contracts]
            day_states = catalog.list_fetch_states(con, interval="day", tokens=tokens)
            minute_states = catalog.list_fetch_states(con, interval="minute", tokens=tokens)
            fetch_now = datetime.now(auth.IST)

            day_items = planner.build_queue(
                series_list,
                ranks,
                day_states,
                fetch_now,
                limit=max_series,
                interval="day",
                market=market_id,
                types=types,
            )
            series_by_key = {(s.underlying, s.expiry): s for s in series_list}
            queue_series = [
                series_by_key[(item.underlying, item.expiry)]
                for item in day_items
                if (item.underlying, item.expiry) in series_by_key
            ]

            day_results = fetch_mod.run_fetch(
                client,
                con,
                queue_series,
                day_states,
                now=fetch_now,
                max_series=max_series,
                interval="day",
                series_root=mpaths.day_dir,
                day_dir=mpaths.day_dir,
                staging_dir=mpaths.staging_dir,
                market=market_id,
                interrupt=ctrl,
            )
            print(
                f"  day fetch: series={day_results.series_done} "
                f"fetched={day_results.contracts_fetched} err={day_results.errors}"
            )

            minute_results = fetch_mod.run_fetch(
                client,
                con,
                queue_series,
                minute_states,
                now=datetime.now(auth.IST),
                max_series=max_series,
                interval="minute",
                series_root=mpaths.minute_dir,
                minute_dir=mpaths.minute_dir,
                day_dir=mpaths.day_dir,
                staging_dir=mpaths.staging_dir,
                backfill_days=backfill_days,
                market=market_id,
                interrupt=ctrl,
            )
            print(
                f"  minute fetch: series={minute_results.series_done} "
                f"fetched={minute_results.contracts_fetched} err={minute_results.errors}"
            )

            from . import status as status_mod

            rows = status_mod.series_status(
                con,
                include_all=False,
                interval="minute",
                day_dir=mpaths.day_dir,
                minute_dir=mpaths.minute_dir,
                market=market_id,
                types=types,
            )
            eod_keys = {(s.underlying, s.expiry) for s in queue_series}
            rows = [r for r in rows if (r.underlying, r.expiry) in eod_keys]
            totals = status_mod.totals(rows) if rows else None
            if totals is not None:
                print(
                    f"  status: {totals.series} series, {totals.complete_pct:.0f}% complete, "
                    f"{totals.pending_requests} pending"
                )

            marker = mark_eod_done(market_id, trade_date)
            print(f"  done marker: {marker}")

            return {
                "market": market_id,
                "trade_date": trade_date,
                "daily_status": result.status,
                "day_fetched": day_results.contracts_fetched,
                "day_errors": day_results.errors,
                "minute_fetched": minute_results.contracts_fetched,
                "minute_errors": minute_results.errors,
                "series": len(queue_series),
                "done_marker": str(marker),
            }
        finally:
            con.close()
    finally:
        fetch_lock.release(market_id, lock_path=fetch_lock_path)


def run_catchup(
    *,
    markets: Sequence[str] | None = None,
    trade_date: date | None = None,
    buffer: timedelta = DEFAULT_READY_BUFFER,
    now: datetime | None = None,
    force: bool = False,
    dry_run: bool = False,
    max_series: int | None = None,
    backfill_days: int | None = planner.BACKFILL_WINDOW_DAYS,
    types: str = "all",
    sleep: Callable[[float], None] = time.sleep,
    should_stop: Callable[[], str | None] | None = None,
    interrupt: object | None = None,
    eod_dir: Path | None = None,
    lock_path: Path | None = None,
) -> list[dict]:
    """Run EOD only for markets already past ready that lack a ``.done`` marker.

    Acquires ``catchup.lock`` (unless ``dry_run``) so tray/manual catch-ups
    do not overlap. Does not sleep for readiness.
    """
    now = _aware_ist(now)
    trade_date = trade_date if trade_date is not None else resolve_trade_date(now)
    candidate = list(markets) if markets is not None else list(DEFAULT_MARKET_ORDER)
    missed = missed_markets(
        now=now,
        trade_date=trade_date,
        markets=candidate,
        buffer=buffer,
        eod_dir=eod_dir,
    )
    print(
        f"catch-up trade_date={ymd(trade_date)} "
        f"missed={','.join(missed) if missed else '(none)'}"
    )
    if not missed:
        return []

    if dry_run:
        return run_eod(
            markets=missed,
            trade_date=trade_date,
            buffer=buffer,
            now=now,
            no_wait=True,
            force=force,
            dry_run=True,
            max_series=max_series,
            backfill_days=backfill_days,
            types=types,
            sleep=sleep,
            should_stop=should_stop,
            interrupt=interrupt,
            fetch_cmd="ohlcv eod --catch-up",
        )

    if not try_acquire_catchup_lock(lock_path=lock_path):
        raise CatchupInProgress(
            f"catch-up already running (lock {lock_path or CATCHUP_LOCK_PATH})"
        )
    try:
        return run_eod(
            markets=missed,
            trade_date=trade_date,
            buffer=buffer,
            now=datetime.now(auth.IST),
            no_wait=True,
            force=force,
            dry_run=False,
            max_series=max_series,
            backfill_days=backfill_days,
            types=types,
            sleep=sleep,
            should_stop=should_stop,
            interrupt=interrupt,
            fetch_cmd="ohlcv eod --catch-up",
        )
    finally:
        release_catchup_lock(lock_path=lock_path)


def run_eod(
    *,
    markets: Sequence[str] | None = None,
    trade_date: date | None = None,
    buffer: timedelta = DEFAULT_READY_BUFFER,
    now: datetime | None = None,
    no_wait: bool = False,
    force: bool = False,
    dry_run: bool = False,
    max_series: int | None = None,
    backfill_days: int | None = planner.BACKFILL_WINDOW_DAYS,
    types: str = "all",
    sleep: Callable[[float], None] = time.sleep,
    should_stop: Callable[[], str | None] | None = None,
    interrupt: object | None = None,
    fetch_cmd: str = "ohlcv eod",
) -> list[dict]:
    """Orchestrate EOD across markets in DEFAULT_MARKET_ORDER (filtered)."""
    now = _aware_ist(now)
    trade_date = trade_date if trade_date is not None else resolve_trade_date(now)
    market_ids = list(markets) if markets is not None else list(DEFAULT_MARKET_ORDER)

    if not dry_run:
        ensure_token()

    catalog_counts: dict[str, tuple[int, int]] = {}
    for mid in market_ids:
        mpaths = paths_for(mid)
        if not mpaths.catalog_path.exists():
            catalog_counts[mid] = (0, 0)
            continue
        try:
            con = catalog.connect(mpaths.catalog_path, read_only=True)
            try:
                catalog_counts[mid] = count_eod_buckets(
                    con, trade_date, market=mid, types=types
                )
            finally:
                con.close()
        except Exception:
            catalog_counts[mid] = (0, 0)

    plan = build_plan(
        trade_date=trade_date,
        markets=market_ids,
        buffer=buffer,
        now=now,
        catalog_counts=catalog_counts,
    )
    print(format_plan(plan))
    print()

    summaries: list[dict] = []
    for mid in market_ids:
        print(f"=== {mid.upper()} ===")
        try:
            summary = run_market_eod(
                mid,
                trade_date=trade_date,
                buffer=buffer,
                now=datetime.now(auth.IST),
                no_wait=no_wait,
                force=force,
                dry_run=dry_run,
                max_series=max_series,
                backfill_days=backfill_days,
                types=types,
                sleep=sleep,
                should_stop=should_stop,
                interrupt=interrupt,
                fetch_cmd=fetch_cmd,
            )
            summaries.append(summary)
            if dry_run:
                print(
                    f"  dry-run: series={summary.get('series', 0)} "
                    f"expiry_today={summary.get('expiry_today', 0)} "
                    f"unfinished={summary.get('unfinished', 0)}"
                )
        except EodSkipped as exc:
            print(f"  skipped: {exc}")
            summaries.append({"market": mid, "skipped": True, "reason": str(exc)})
        except EodNonSession as exc:
            print(f"  non-session: {exc}")
            summaries.append({"market": mid, "non_session": True, "reason": str(exc)})
            # First market holiday → stop the whole run.
            if mid == market_ids[0]:
                print("first market is non-session; stopping remaining markets")
                break
        except (auth.NotLoggedIn, SessionExpired) as exc:
            raise
        except RetryExhausted as exc:
            summaries.append({"market": mid, "error": str(exc)})
            print(f"  error: {exc}")
        print()
    return summaries
