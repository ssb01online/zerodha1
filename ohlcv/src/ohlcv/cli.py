"""CLI entry: uv run ohlcv ..."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from kiteconnect.exceptions import TokenException

from kitetools import auth

from .paths import ymd


def _login_exit(exc: BaseException) -> None:
    raise SystemExit(f"{exc}\nRun: uv run kitetools login") from exc


def _parse_ymd(text: str) -> date:
    text = text.strip()
    if len(text) == 8 and text.isdigit():
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    return date.fromisoformat(text)


def _optional_log_path(args: argparse.Namespace) -> Path | None:
    raw = getattr(args, "log_file", None)
    if not raw:
        return None
    return Path(raw)


def _market_id(args: argparse.Namespace) -> str:
    return (getattr(args, "market", None) or "nfo").strip().lower()


def _add_market_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--market",
        choices=("nfo", "mcx", "cds"),
        default="nfo",
        help="Exchange market root under data/ohlcv/<market>/ (default: nfo).",
    )


def cmd_probe(args: argparse.Namespace) -> None:
    from . import probe

    try:
        path = probe.run_probe(token=args.token, expired=args.expired)
    except (auth.NotLoggedIn, TokenException) as exc:
        _login_exit(exc)
    print(f"Wrote {path}", file=sys.stderr)


def _run_sync(con, market_id: str = "nfo") -> tuple[int, object]:
    from . import archive as archive_mod
    from . import universe

    from instruments import ensure_cache

    ensure_cache()
    archive_path = archive_mod.ensure_archive()
    count = universe.sync_contracts(con, market=market_id)
    return count, archive_path


def cmd_sync(args: argparse.Namespace) -> None:
    from . import catalog
    from .markets import get_market
    from .paths import paths_for

    market_id = _market_id(args)
    profile = get_market(market_id)
    catalog_path = paths_for(market_id).catalog_path

    started = datetime.now(auth.IST)
    try:
        con = catalog.connect(catalog_path)
    except catalog.CatalogLocked as exc:
        raise SystemExit(str(exc)) from exc
    try:
        catalog.ensure_schema(con)
        run_id = catalog.start_run(con, "sync", started_at=started.replace(tzinfo=None))
        try:
            count, archive_path = _run_sync(con, market_id=market_id)
        except FileNotFoundError as exc:
            catalog.finish_run(
                con,
                run_id,
                finished_at=datetime.now(auth.IST).replace(tzinfo=None),
                errors=1,
                note=str(exc),
            )
            raise SystemExit(str(exc)) from exc
        except (auth.NotLoggedIn, TokenException) as exc:
            catalog.finish_run(
                con,
                run_id,
                finished_at=datetime.now(auth.IST).replace(tzinfo=None),
                errors=1,
                note=str(exc),
            )
            _login_exit(exc)
        finished = datetime.now(auth.IST).replace(tzinfo=None)
        catalog.finish_run(
            con,
            run_id,
            finished_at=finished,
            note=f"market={market_id} contracts={count} archive={archive_path}",
        )
        types = (
            "all with expiry"
            if profile.sync_instrument_types is None
            else "+".join(sorted(profile.sync_instrument_types))
        )
        print(f"Synced {count} {profile.exchange} contracts ({types})")
        print(f"Market: {market_id}")
        print(f"Archive: {archive_path}")
        print(f"Catalog: {catalog_path}")
    finally:
        con.close()


def _print_ranks(con) -> None:
    from . import rank as rank_mod

    ranks = sorted(
        rank_mod.load_ranks(con).values(),
        key=lambda r: r.traded_value,
        reverse=True,
    )
    source = ranks[0].source if ranks else "-"
    print()
    print(
        f"{'#':<4} {'underlying':<16} {'traded_value':>16} "
        f"{'volume':>12} {'close':>10}  source={source}"
    )
    print("-" * 72)
    for i, r in enumerate(ranks[:20], start=1):
        print(
            f"{i:<4} {r.underlying:<16} {r.traded_value:>16,.0f} "
            f"{r.volume:>12,} {r.close:>10.2f}"
        )


def cmd_rank(args: argparse.Namespace) -> None:
    from . import catalog
    from . import rank as rank_mod
    from .client import KiteClient, SessionExpired
    from .paths import CATALOG_PATH

    started = datetime.now(auth.IST)
    try:
        con = catalog.connect()
    except catalog.CatalogLocked as exc:
        raise SystemExit(str(exc)) from exc

    client = None
    try:
        catalog.ensure_schema(con)
        run_id = catalog.start_run(con, "rank", started_at=started.replace(tzinfo=None))
        try:
            count, archive_path = _run_sync(con)
            print(f"Synced {count} NFO contracts (archive {archive_path})")
            # Prefer final snapshot (no Kite). Fall back to FUT day candles if needed.
            snap = catalog.latest_snapshot(con, status="final")
            need_kite = snap is None or args.force
            if need_kite and snap is None:
                try:
                    kite = auth.get_kite()
                except (auth.NotLoggedIn, TokenException) as exc:
                    catalog.finish_run(
                        con,
                        run_id,
                        finished_at=datetime.now(auth.IST).replace(tzinfo=None),
                        errors=1,
                        note=str(exc),
                    )
                    _login_exit(exc)
                client = KiteClient(kite)
            # When force and a final snap exists, re-rank from the snap (still no hist).
            if args.force and snap is not None:
                result = rank_mod.rank_from_snapshot(con, snap.trade_date)
            else:
                result = rank_mod.refresh_rank(client, con, force=args.force)
        except SessionExpired as exc:
            catalog.finish_run(
                con,
                run_id,
                finished_at=datetime.now(auth.IST).replace(tzinfo=None),
                requests=client.counters.requests if client else 0,
                errors=1,
                note=str(exc),
            )
            _login_exit(exc)
        except (auth.NotLoggedIn, TokenException) as exc:
            catalog.finish_run(
                con,
                run_id,
                finished_at=datetime.now(auth.IST).replace(tzinfo=None),
                errors=1,
                note=str(exc),
            )
            _login_exit(exc)

        finished = datetime.now(auth.IST).replace(tzinfo=None)
        requests = client.counters.requests if client else 0
        retries = client.counters.retries if client else 0
        errors = (client.counters.errors if client else 0) + len(result.errors)
        catalog.finish_run(
            con,
            run_id,
            finished_at=finished,
            requests=requests,
            retries=retries,
            errors=errors,
            note=(
                f"skipped={result.skipped} upserted={result.upserted} "
                f"trade_date={result.trade_date} source={result.source}"
            ),
        )

        if result.skipped:
            print(
                f"Rank fresh (trade_date={result.trade_date}, source={result.source}); "
                "skipped (use --force)"
            )
        else:
            print(
                f"Ranked {result.upserted} underlyings "
                f"(trade_date={result.trade_date}, source={result.source}, "
                f"requests={requests}, retries={retries})"
            )
        if result.errors:
            print(f"Errors ({len(result.errors)}):", file=sys.stderr)
            for err in result.errors[:20]:
                print(f"  {err}", file=sys.stderr)

        _print_ranks(con)
        print(f"Catalog: {CATALOG_PATH}")
    finally:
        con.close()


def cmd_daily(args: argparse.Namespace) -> None:
    from . import catalog
    from . import rank as rank_mod
    from . import snapshot
    from .client import KiteClient, RetryExhausted, SessionExpired
    from .paths import CATALOG_PATH, DAILY_DIR
    from .progress import InterruptController, ProgressReporter, setup_run_logging

    if args.show is not None:
        _cmd_daily_show(args)
        return

    log_path = setup_run_logging("daily", log_file=_optional_log_path(args))
    reporter = ProgressReporter(logger=logging.getLogger("ohlcv"))
    interrupt = InterruptController()
    interrupt.install(on_soft_stop=reporter.soft_stop_announced)
    started = datetime.now(auth.IST)
    t0 = time.monotonic()
    con = None
    try:
        try:
            con = catalog.connect()
        except catalog.CatalogLocked as exc:
            raise SystemExit(str(exc)) from exc

        client = None
        catalog.ensure_schema(con)
        run_id = catalog.start_run(con, "daily", started_at=started.replace(tzinfo=None))
        try:
            count, archive_path = _run_sync(con)
            reporter.note(f"Synced {count} NFO contracts (archive {archive_path})")
            contracts = snapshot.live_contracts(con)
            dry = snapshot.estimate_dry_run(contracts)
            if args.dry_run:
                catalog.finish_run(
                    con,
                    run_id,
                    finished_at=datetime.now(auth.IST).replace(tzinfo=None),
                    note=f"dry_run contracts={dry.contracts} batches={dry.batches}",
                )
                print(
                    f"Dry run: {dry.contracts} contracts, {dry.batches} quote batches, "
                    f"~{dry.estimated_seconds / 60:.1f} min"
                )
                print(f"Catalog: {CATALOG_PATH}")
                print(f"Log: {log_path}")
                return

            try:
                kite = auth.get_kite()
            except (auth.NotLoggedIn, TokenException) as exc:
                catalog.finish_run(
                    con,
                    run_id,
                    finished_at=datetime.now(auth.IST).replace(tzinfo=None),
                    errors=1,
                    note=str(exc),
                )
                _login_exit(exc)

            client = KiteClient(kite)

            def _on_batch(bi: int, bt: int, done: int, total: int) -> None:
                reporter.batch(
                    batch_index=bi,
                    batch_total=bt,
                    contracts_done=done,
                    contracts_total=total,
                    requests=client.counters.requests,
                    elapsed_s=time.monotonic() - t0,
                )

            result = snapshot.take_snapshot(
                client,
                contracts,
                should_stop=interrupt.should_stop,
                on_batch=_on_batch,
            )
            reporter.clear_heartbeat()
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
            rank_result = rank_mod.rank_from_snapshot(con, result.trade_date)
        except snapshot.SnapshotRejected as exc:
            catalog.finish_run(
                con,
                run_id,
                finished_at=datetime.now(auth.IST).replace(tzinfo=None),
                requests=client.counters.requests if client else 0,
                retries=client.counters.retries if client else 0,
                errors=1,
                note=str(exc),
            )
            raise SystemExit(str(exc)) from exc
        except RetryExhausted as exc:
            catalog.finish_run(
                con,
                run_id,
                finished_at=datetime.now(auth.IST).replace(tzinfo=None),
                requests=client.counters.requests if client else 0,
                retries=client.counters.retries if client else 0,
                errors=1,
                note=str(exc),
            )
            raise SystemExit(f"quote retries exhausted: {exc}") from exc
        except SessionExpired as exc:
            catalog.finish_run(
                con,
                run_id,
                finished_at=datetime.now(auth.IST).replace(tzinfo=None),
                requests=client.counters.requests if client else 0,
                errors=1,
                note=str(exc),
            )
            _login_exit(exc)
        except (auth.NotLoggedIn, TokenException) as exc:
            catalog.finish_run(
                con,
                run_id,
                finished_at=datetime.now(auth.IST).replace(tzinfo=None),
                errors=1,
                note=str(exc),
            )
            _login_exit(exc)

        finished = datetime.now(auth.IST).replace(tzinfo=None)
        catalog.finish_run(
            con,
            run_id,
            finished_at=finished,
            requests=client.counters.requests if client else 0,
            retries=client.counters.retries if client else 0,
            errors=(client.counters.errors if client else 0) + len(rank_result.errors),
            note=(
                f"trade_date={result.trade_date} status={result.status} "
                f"contracts={result.contracts} traded={result.traded_contracts} "
                f"missing={result.missing} path={result.path}"
            ),
        )
        print(
            f"Snapshot {ymd(result.trade_date)} status={result.status} "
            f"contracts={result.contracts} traded={result.traded_contracts} "
            f"missing={result.missing} volume={result.total_volume:,}"
        )
        print(f"Wrote {result.path}")
        print(
            f"Ranked {rank_result.upserted} underlyings "
            f"(source={rank_result.source}, "
            f"requests={client.counters.requests if client else 0})"
        )
        _print_ranks(con)
        print(f"Daily dir: {DAILY_DIR}")
        print(f"Catalog: {CATALOG_PATH}")
        print(f"Log: {log_path}")
    finally:
        interrupt.restore()
        if con is not None:
            con.close()


def _cmd_daily_show(args: argparse.Namespace) -> None:
    from . import catalog
    from . import read as read_mod
    from .paths import CATALOG_PATH, DAILY_DIR

    meta = None
    con = None
    try:
        if CATALOG_PATH.is_file():
            try:
                con = catalog.connect()
            except catalog.CatalogLocked as exc:
                raise SystemExit(str(exc)) from exc
            catalog.ensure_schema(con)

        if args.show:
            trade_date = _parse_ymd(args.show)
            if con is not None:
                meta = catalog.get_snapshot(con, trade_date)
        else:
            if con is not None:
                meta = catalog.latest_snapshot(con)
            if meta is None:
                files = sorted(DAILY_DIR.glob("*.parquet")) if DAILY_DIR.is_dir() else []
                if not files:
                    raise SystemExit("no daily snapshots recorded")
                trade_date = _parse_ymd(files[-1].stem)
            else:
                trade_date = meta.trade_date

        if meta is not None:
            print(
                f"Catalog: {ymd(meta.trade_date)} status={meta.status} "
                f"contracts={meta.contracts} traded={meta.traded_contracts} "
                f"missing={meta.missing}"
            )

        rows = read_mod.daily(trade_date)
        traded = sum(1 for r in rows if r.get("traded") or (r.get("volume") or 0) > 0)
        total_vol = sum(int(r.get("volume") or 0) for r in rows)
        by_ul: dict[str, float] = {}
        for r in rows:
            ul = str(r["underlying"])
            by_ul[ul] = by_ul.get(ul, 0.0) + float(r.get("traded_value") or 0.0)
        print(f"File: {read_mod.daily_path(trade_date)}")
        print(f"Rows: {len(rows)} traded={traded} volume={total_vol:,}")
        print()
        print(f"{'#':<4} {'underlying':<16} {'traded_value':>16}")
        print("-" * 40)
        for i, (ul, tv) in enumerate(
            sorted(by_ul.items(), key=lambda kv: kv[1], reverse=True)[:20],
            start=1,
        ):
            print(f"{i:<4} {ul:<16} {tv:>16,.0f}")
        print(f"Catalog: {CATALOG_PATH}")
    except FileNotFoundError as exc:
        raise SystemExit(str(exc)) from exc
    finally:
        if con is not None:
            con.close()


def _merge_series_by_key(*groups: list) -> list:
    """Union Series lists by (underlying, expiry), preserving first-seen contract set."""
    by_key: dict[tuple[str, date], object] = {}
    for group in groups:
        for s in group:
            by_key.setdefault((s.underlying, s.expiry), s)
    return list(by_key.values())


def cmd_fetch(args: argparse.Namespace) -> None:
    """Day candles (~2000d) then minute candles (--days window) for the same queue."""
    import time as time_mod

    from . import catalog
    from . import fetch as fetch_mod
    from . import planner
    from . import rank as rank_mod
    from . import universe
    from .client import KiteClient, SessionExpired
    from .paths import CATALOG_PATH, DAY_DIR, MINUTE_DIR, ymd as paths_ymd
    from .progress import InterruptController, ProgressReporter, setup_run_logging

    try:
        backfill_days = planner.parse_days_arg(getattr(args, "days", None))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    days_label = "all" if backfill_days is None else str(backfill_days)

    log_path = setup_run_logging("fetch", log_file=_optional_log_path(args))
    reporter = ProgressReporter(logger=logging.getLogger("ohlcv"))
    interrupt = InterruptController()
    interrupt.install(on_soft_stop=reporter.soft_stop_announced)

    started = datetime.now(auth.IST)
    expiry_filter = _parse_ymd(args.expiry) if args.expiry else None
    con = None
    try:
        try:
            con = catalog.connect()
        except catalog.CatalogLocked as exc:
            raise SystemExit(str(exc)) from exc

        client = None
        catalog.ensure_schema(con)
        run_id = catalog.start_run(con, "fetch", started_at=started.replace(tzinfo=None))
        try:
            count, archive_path = _run_sync(con)
        except (auth.NotLoggedIn, TokenException) as exc:
            catalog.finish_run(
                con,
                run_id,
                finished_at=datetime.now(auth.IST).replace(tzinfo=None),
                errors=1,
                note=str(exc),
            )
            _login_exit(exc)
        except FileNotFoundError as exc:
            catalog.finish_run(
                con,
                run_id,
                finished_at=datetime.now(auth.IST).replace(tzinfo=None),
                errors=1,
                note=str(exc),
            )
            raise SystemExit(str(exc)) from exc

        reporter.note(f"Synced {count} NFO contracts (archive {archive_path})")
        series_day = universe.select_series(con, interval="day")
        series_min = universe.select_series(con, interval="minute")
        series = _merge_series_by_key(series_day, series_min)
        ranks = rank_mod.load_ranks(con)
        tokens = [c.token for s in series for c in s.contracts]
        day_states = catalog.list_fetch_states(con, interval="day", tokens=tokens)
        minute_states = catalog.list_fetch_states(con, interval="minute", tokens=tokens)
        now = datetime.now(auth.IST)

        # Queue order from ranks; pending counts summed for dry-run display.
        day_items = planner.build_queue(
            series,
            ranks,
            day_states,
            now,
            expiry_filter=expiry_filter,
            underlying_filter=args.underlying,
            limit=args.max_series,
            interval="day",
        )
        series_by_key = {(s.underlying, s.expiry): s for s in series}
        queue_series = [
            series_by_key[(item.underlying, item.expiry)]
            for item in day_items
            if (item.underlying, item.expiry) in series_by_key
        ]
        minute_items = planner.build_queue(
            queue_series,
            ranks,
            minute_states,
            now,
            interval="minute",
            backfill_days=backfill_days,
        )
        minute_by_key = {(i.underlying, i.expiry): i for i in minute_items}

        if args.dry_run:
            skip_cache: dict = {}
            traded_cache: dict = {}
            print()
            print(
                f"{'expiry':<10} {'underlying':<16} {'contracts':>9} "
                f"{'day_req':>8} {'min_req':>8} {'skip~':>6} {'est_min':>8}"
            )
            print("-" * 78)
            total_req = 0
            total_sec = 0.0
            total_skip = 0
            for day_item, s in zip(day_items, queue_series):
                min_item = minute_by_key.get((day_item.underlying, day_item.expiry))
                min_pending = min_item.pending_requests if min_item else 0
                day_skip = fetch_mod.count_skippable(
                    con,
                    s,
                    day_states,
                    now=now,
                    untraded_cache=skip_cache,
                    interval="day",
                )
                min_skip = fetch_mod.count_skippable(
                    con,
                    s,
                    minute_states,
                    now=now,
                    untraded_cache=skip_cache,
                    traded_days_cache=traded_cache,
                    interval="minute",
                    backfill_days=backfill_days,
                    day_dir=DAY_DIR,
                )
                adj_day = max(0, day_item.pending_requests - day_skip)
                adj_min = max(0, min_pending - min_skip)
                adj_req = adj_day + adj_min
                adj_sec = adj_req * planner.request_seconds()
                skip_n = day_skip + min_skip
                total_req += adj_req
                total_sec += adj_sec
                total_skip += skip_n
                print(
                    f"{paths_ymd(day_item.expiry):<10} {day_item.underlying:<16} "
                    f"{day_item.contracts:>9} {adj_day:>8} {adj_min:>8} "
                    f"{skip_n:>6} {adj_sec / 60:>8.1f}"
                )
            catalog.finish_run(
                con,
                run_id,
                finished_at=datetime.now(auth.IST).replace(tzinfo=None),
                note=(
                    f"dry_run days={days_label} series={len(day_items)} "
                    f"requests~={total_req} skip~={total_skip}"
                ),
            )
            print()
            print(
                f"Dry run (day then minute, --days={days_label}): {len(day_items)} series, "
                f"~{total_req} requests (~{total_skip} skips), ~{total_sec / 60:.1f} min"
            )
            print(f"Catalog: {CATALOG_PATH}")
            print(f"Log: {log_path}")
            return

        try:
            kite = auth.get_kite()
        except (auth.NotLoggedIn, TokenException) as exc:
            catalog.finish_run(
                con,
                run_id,
                finished_at=datetime.now(auth.IST).replace(tzinfo=None),
                errors=1,
                note=str(exc),
            )
            _login_exit(exc)

        client = KiteClient(kite)
        contracts_total = sum(len(s.contracts) for s in queue_series)
        # Heartbeat totals count both passes (day + minute) over the same contracts.
        reporter.set_totals(contracts_total=contracts_total * 2)
        reporter.note(
            f"Fetch queue: {len(queue_series)} series, {contracts_total} contracts "
            f"(day ~{planner.DAY_BACKFILL_DAYS}d then minute --days={days_label})"
        )

        deadline = None
        if args.max_minutes is not None:
            deadline = time_mod.monotonic() + float(args.max_minutes) * 60.0

        def _on_series_start(idx: int, total: int, s: universe.Series) -> None:
            reporter.series_start(
                series_index=idx,
                series_total=total,
                underlying=s.underlying,
                expiry=s.expiry,
                contracts=len(s.contracts),
            )

        def _on_series(idx: int, total: int, result: fetch_mod.SeriesResult) -> None:
            reporter.series_done(
                series_index=idx,
                series_total=total,
                underlying=result.underlying,
                expiry=result.expiry,
                contracts=result.contracts,
                fetched=result.fetched,
                skipped=result.skipped,
                errors=result.errors,
                rows=result.rows,
                elapsed_s=result.elapsed_s,
                locked=result.locked,
                stop_reason=result.stop_reason,
            )

        run_day = None
        run_min = None
        try:
            reporter.note("Pass 1/2: day candles")
            run_day = fetch_mod.run_fetch(
                client,
                con,
                queue_series,
                day_states,
                now=now,
                max_series=None,  # already applied when building queue
                max_minutes=args.max_minutes,
                interval="day",
                series_root=DAY_DIR,
                day_dir=DAY_DIR,
                on_series=_on_series,
                on_contract=reporter.contract,
                on_series_start=_on_series_start,
                interrupt=interrupt,
                deadline_monotonic=deadline,
            )
            if run_day.stopped_reason is None:
                reporter.note("Pass 2/2: minute candles")
                run_min = fetch_mod.run_fetch(
                    client,
                    con,
                    queue_series,
                    minute_states,
                    now=now,
                    max_series=None,
                    max_minutes=args.max_minutes,
                    interval="minute",
                    series_root=MINUTE_DIR,
                    day_dir=DAY_DIR,
                    backfill_days=backfill_days,
                    on_series=_on_series,
                    on_contract=reporter.contract,
                    on_series_start=_on_series_start,
                    interrupt=interrupt,
                    deadline_monotonic=deadline,
                )
        except SessionExpired as exc:
            catalog.finish_run(
                con,
                run_id,
                finished_at=datetime.now(auth.IST).replace(tzinfo=None),
                requests=client.counters.requests,
                retries=client.counters.retries,
                errors=client.counters.errors + 1,
                note=f"session_expired {exc}",
            )
            _login_exit(exc)

        def _sum_attr(attr: str) -> int:
            total = 0
            for run in (run_day, run_min):
                if run is not None:
                    total += getattr(run, attr)
            return total

        locked = []
        if run_day is not None:
            locked.extend(run_day.locked)
        if run_min is not None:
            locked.extend(run_min.locked)
        stopped = None
        if run_day is not None and run_day.stopped_reason:
            stopped = run_day.stopped_reason
        elif run_min is not None and run_min.stopped_reason:
            stopped = run_min.stopped_reason

        finished = datetime.now(auth.IST).replace(tzinfo=None)
        note_parts = [
            f"days={days_label}",
            f"series={len(queue_series)}",
            f"day_done={run_day.series_done if run_day else 0}",
            f"min_done={run_min.series_done if run_min else 0}",
            f"fetched={_sum_attr('contracts_fetched')}",
            f"skipped={_sum_attr('contracts_skipped')}",
            f"rows={_sum_attr('rows_written')}",
            f"errors={_sum_attr('errors')}",
            f"gave_up={_sum_attr('gave_up')}",
        ]
        if locked:
            note_parts.append(
                "locked=" + ",".join(f"{u}:{paths_ymd(e)}" for u, e in locked)
            )
        if stopped:
            note_parts.append(f"stopped={stopped}")
        catalog.finish_run(
            con,
            run_id,
            finished_at=finished,
            requests=client.counters.requests,
            retries=client.counters.retries,
            errors=client.counters.errors + _sum_attr("errors"),
            note=" ".join(note_parts),
        )

        reporter.clear_heartbeat()
        print()
        print(
            f"Done (day+minute, --days={days_label}): "
            f"day_series={run_day.series_done if run_day else 0}, "
            f"min_series={run_min.series_done if run_min else 0}, "
            f"fetched={_sum_attr('contracts_fetched')}, "
            f"skipped={_sum_attr('contracts_skipped')}, "
            f"rows={_sum_attr('rows_written'):,}, "
            f"errors={_sum_attr('errors')}, gave_up={_sum_attr('gave_up')}"
        )
        if locked:
            print(f"Locked files ({len(locked)}): close notebooks and re-run")
            for u, e in locked:
                print(f"  {u} {paths_ymd(e)}")
        if stopped:
            print(f"Stopped: {stopped}")
        print(f"Day dir: {DAY_DIR}")
        print(f"Minute dir: {MINUTE_DIR}")
        print(f"Catalog: {CATALOG_PATH}")
        print(f"Log: {log_path}")
    finally:
        interrupt.restore()
        if con is not None:
            con.close()


def _catalog_ro():
    """Open catalog read-only; exit with a clear message on lock."""
    from . import catalog

    try:
        return catalog.connect(read_only=True)
    except catalog.CatalogLocked as exc:
        raise SystemExit(
            "a daily/fetch run (or notebook) holds catalog.duckdb; "
            "retry after it finishes"
        ) from exc
    except Exception as exc:
        # DuckDB raises IOException when the file does not exist under read_only.
        msg = str(exc).lower()
        if "cannot open" in msg or "no such file" in msg or "does not exist" in msg:
            from .paths import CATALOG_PATH

            raise SystemExit(
                f"catalog not found: {CATALOG_PATH}\n"
                "Run: uv run ohlcv sync"
            ) from exc
        raise


def cmd_status(args: argparse.Namespace) -> None:
    from collections import defaultdict

    from . import catalog
    from . import status as status_mod
    from .paths import CATALOG_PATH, ymd as paths_ymd

    interval = getattr(args, "interval", "minute") or "minute"
    if interval not in ("minute", "day"):
        raise SystemExit(f"unsupported --interval: {interval}")
    expiry_filter = _parse_ymd(args.expiry) if args.expiry else None
    con = _catalog_ro()
    try:
        rows = status_mod.series_status(
            con,
            include_all=args.all,
            expiry=expiry_filter,
            underlying=args.underlying,
            interval=interval,
        )
        print(f"Interval: {interval}")
        if args.limit is not None and args.limit >= 0:
            rows = rows[: args.limit]

        print(
            f"{'expiry':<10} {'underlying':<14} {'ctr':>5} {'done%':>6} "
            f"{'part':>4} {'err':>4} {'give':>4} {'miss':>4} "
            f"{'pend':>5} {'est_m':>6} {'MB':>7} {'rows':>8} {'watermark':<16}"
        )
        print("-" * 110)
        for r in rows:
            wm = status_mod.watermark_label(r.max_fetched_to)
            mb = r.file_bytes / (1024 * 1024) if r.file_bytes else 0.0
            print(
                f"{paths_ymd(r.expiry):<10} {r.underlying:<14} {r.contracts:>5} "
                f"{r.complete_pct:>5.0f}% {r.partial:>4} {r.error:>4} "
                f"{r.gave_up:>4} {r.missing:>4} {r.pending_requests:>5} "
                f"{r.estimated_minutes:>6.1f} {mb:>7.1f} {r.file_rows:>8,} {wm:<16}"
            )

        by_expiry: dict = defaultdict(list)
        for r in rows:
            by_expiry[r.expiry].append(r)
        print()
        print("Subtotals by expiry:")
        for expiry in sorted(by_expiry):
            group = by_expiry[expiry]
            t = status_mod.totals(group)
            print(
                f"  {paths_ymd(expiry)}: {t.series} series, {t.contracts} contracts, "
                f"{t.complete_pct:.0f}% complete, {t.pending_requests} pending, "
                f"~{t.estimated_minutes:.1f} min, {status_mod.format_bytes(t.file_bytes)}"
            )
        grand = status_mod.totals(rows)
        print(
            f"Total: {grand.series} series, {grand.contracts} contracts, "
            f"{grand.complete_pct:.0f}% complete, {grand.pending_requests} pending, "
            f"~{grand.estimated_minutes:.1f} min, {status_mod.format_bytes(grand.file_bytes)}"
        )

        print()
        print("Daily snapshots (latest 5):")
        snaps = catalog.list_snapshots(con, limit=5)
        if not snaps:
            print("  (none)")
        else:
            for s in snaps:
                print(
                    f"  {paths_ymd(s.trade_date)} status={s.status} "
                    f"contracts={s.contracts} traded={s.traded_contracts} "
                    f"missing={s.missing} volume={s.total_volume:,}"
                )
        gaps = status_mod.snapshot_gaps(con)
        if gaps:
            gap_text = ", ".join(paths_ymd(d) for d in gaps[:20])
            extra = f" (+{len(gaps) - 20} more)" if len(gaps) > 20 else ""
            print(
                f"Missing final snapshots (weekdays; may include holidays): "
                f"{gap_text}{extra}"
            )
        else:
            print("Missing final snapshots: none")

        print()
        print(f"Recent runs (last {args.runs}):")
        runs = catalog.list_runs(con, limit=args.runs)
        if not runs:
            print("  (none)")
        else:
            for run in runs:
                if run.finished_at is None:
                    dur = "running or crashed"
                else:
                    secs = (run.finished_at - run.started_at).total_seconds()
                    if secs < 60:
                        dur = f"{secs:.0f}s"
                    else:
                        dur = f"{secs / 60:.1f}m"
                note = (run.note or "")[:60]
                started = run.started_at.strftime("%Y-%m-%d %H:%M")
                print(
                    f"  #{run.run_id} {run.command:<8} {started} {dur:>16} "
                    f"req={run.requests} retry={run.retries} err={run.errors}"
                    + (f"  {note}" if note else "")
                )
        print(f"Catalog: {CATALOG_PATH}")
    finally:
        con.close()


def cmd_show(args: argparse.Namespace) -> None:
    from . import catalog
    from . import read as read_mod
    from . import status as status_mod
    from .paths import CATALOG_PATH, sanitize_underlying, ymd as paths_ymd

    expiry = _parse_ymd(args.expiry)
    underlying = args.underlying.strip()
    if args.symbol and args.token is not None:
        raise SystemExit("use either --symbol or --token, not both")

    con = _catalog_ro()
    try:
        # Resolve catalog name: accept M&M or M_M.
        contracts = catalog.list_contracts(con, expiry=expiry)
        name_map = {
            sanitize_underlying(c.underlying): c.underlying for c in contracts
        }
        # Also search all contracts if expiry filter returned nothing for name map.
        if not name_map:
            name_map = {
                sanitize_underlying(c.underlying): c.underlying
                for c in catalog.list_contracts(con)
            }
        catalog_ul = name_map.get(sanitize_underlying(underlying), underlying)
        series_contracts = [
            c
            for c in catalog.list_contracts(con, underlying=catalog_ul, expiry=expiry)
        ]
        interval = getattr(args, "interval", "minute") or "minute"
        if interval not in ("minute", "day"):
            raise SystemExit(f"unsupported --interval: {interval}")
        path = read_mod.series_file_path(catalog_ul, expiry, interval=interval)
        try:
            file_bytes, file_rows, min_ts, max_ts = read_mod.file_info(path)
        except FileNotFoundError as exc:
            raise SystemExit(str(exc)) from exc

        print(f"Series: {catalog_ul} {paths_ymd(expiry)}  interval={interval}")
        print(f"File: {path}")
        print(
            f"Size: {status_mod.format_bytes(file_bytes)}  rows={file_rows:,}  "
            f"ts=[{status_mod.watermark_label(min_ts)} .. "
            f"{status_mod.watermark_label(max_ts)}]"
        )
        print(f"Catalog contracts: {len(series_contracts)}")

        duck = read_mod.connect()
        try:
            if args.symbol or args.token is not None:
                match = None
                if args.token is not None:
                    match = next(
                        (c for c in series_contracts if c.token == args.token), None
                    )
                elif args.symbol:
                    match = next(
                        (
                            c
                            for c in series_contracts
                            if c.tradingsymbol == args.symbol
                        ),
                        None,
                    )
                state = None
                if match is not None:
                    state = catalog.get_fetch_state(con, match.token, interval)
                    print(
                        f"Contract: {match.tradingsymbol} token={match.token} "
                        f"type={match.instrument_type} strike={match.strike}"
                    )
                elif args.symbol:
                    print(f"Contract: {args.symbol} (not in catalog for this series)")
                else:
                    print(f"Contract: token={args.token} (not in catalog for this series)")

                if state is not None:
                    print(
                        f"fetch_state: status={state.status} "
                        f"fetched_to={status_mod.watermark_label(state.fetched_to)} "
                        f"attempts={state.attempts} rows_total={state.rows_total}"
                    )
                    if state.last_error:
                        print(f"  last_error: {state.last_error[:120]}")
                else:
                    print("fetch_state: (none)")

                day_rows = duck.execute(
                    """
                    SELECT CAST(ts AS DATE) AS trade_date,
                           COUNT(*) AS bars,
                           SUM(volume) AS volume,
                           arg_max(oi, ts) AS last_oi,
                           MIN(ts) AS first_ts,
                           MAX(ts) AS last_ts
                    FROM read_parquet(?)
                    WHERE (? IS NULL OR token = ?)
                      AND (? IS NULL OR tradingsymbol = ?)
                    GROUP BY 1
                    ORDER BY 1
                    """,
                    [
                        str(path),
                        args.token,
                        args.token,
                        args.symbol,
                        args.symbol,
                    ],
                ).fetchall()
                print()
                print(
                    f"{'date':<12} {'bars':>6} {'volume':>12} "
                    f"{'last_oi':>10} {'first':<16} {'last':<16}"
                )
                print("-" * 78)
                if not day_rows:
                    print("  (no bars)")
                for row in day_rows:
                    print(
                        f"{str(row[0])[:10]:<12} {int(row[1]):>6} {int(row[2] or 0):>12,} "
                        f"{int(row[3] or 0):>10,} "
                        f"{status_mod.watermark_label(row[4]):<16} "
                        f"{status_mod.watermark_label(row[5]):<16}"
                    )

                tail_n = max(0, int(args.tail))
                if tail_n > 0:
                    bars = read_mod.series(
                        catalog_ul,
                        expiry,
                        tokens=[args.token] if args.token is not None else None,
                        symbols=[args.symbol] if args.symbol else None,
                    )
                    print()
                    print(f"Last {tail_n} bars:")
                    for d in bars[-tail_n:]:
                        print(
                            f"  {d.get('ts')} o={d.get('open')} h={d.get('high')} "
                            f"l={d.get('low')} c={d.get('close')} "
                            f"v={d.get('volume')} oi={d.get('oi')}"
                        )
            else:
                overview = duck.execute(
                    """
                    SELECT
                        instrument_type,
                        COUNT(DISTINCT token) AS contracts,
                        COUNT(*) AS rows
                    FROM read_parquet(?)
                    GROUP BY instrument_type
                    ORDER BY instrument_type
                    """,
                    [str(path)],
                ).fetchall()
                tokens_with = duck.execute(
                    "SELECT COUNT(DISTINCT token) FROM read_parquet(?)",
                    [str(path)],
                ).fetchone()
                n_with = int(tokens_with[0] or 0) if tokens_with else 0
                print(
                    f"Contracts with data: {n_with} / {len(series_contracts)}"
                )
                print()
                print(f"{'type':<6} {'contracts':>10} {'rows':>12}")
                print("-" * 32)
                for row in overview:
                    print(
                        f"{str(row[0]):<6} {int(row[1]):>10} {int(row[2]):>12,}"
                    )

                top = duck.execute(
                    """
                    SELECT token, ANY_VALUE(tradingsymbol) AS symbol,
                           SUM(volume) AS volume
                    FROM read_parquet(?)
                    GROUP BY token
                    ORDER BY volume DESC
                    LIMIT 10
                    """,
                    [str(path)],
                ).fetchall()
                print()
                print("Top 10 by volume:")
                print(f"{'#':<4} {'symbol':<24} {'token':>10} {'volume':>14}")
                print("-" * 56)
                for i, row in enumerate(top, start=1):
                    print(
                        f"{i:<4} {str(row[1]):<24} {int(row[0]):>10} "
                        f"{int(row[2] or 0):>14,}"
                    )
        finally:
            duck.close()
        print(f"Catalog: {CATALOG_PATH}")
    finally:
        con.close()


def cmd_queue(args: argparse.Namespace) -> None:
    from . import catalog
    from . import planner
    from . import rank as rank_mod
    from . import universe
    from .markets import get_market
    from .paths import paths_for

    market_id = _market_id(args)
    profile = get_market(market_id)
    catalog_path = paths_for(market_id).catalog_path

    started = datetime.now(auth.IST)
    expiry_filter = _parse_ymd(args.expiry) if args.expiry else None
    try:
        con = catalog.connect(catalog_path)
    except catalog.CatalogLocked as exc:
        raise SystemExit(str(exc)) from exc
    try:
        catalog.ensure_schema(con)
        run_id = catalog.start_run(con, "queue", started_at=started.replace(tzinfo=None))
        try:
            count, archive_path = _run_sync(con, market_id=market_id)
        except (auth.NotLoggedIn, TokenException) as exc:
            catalog.finish_run(
                con,
                run_id,
                finished_at=datetime.now(auth.IST).replace(tzinfo=None),
                errors=1,
                note=str(exc),
            )
            _login_exit(exc)
        except FileNotFoundError as exc:
            catalog.finish_run(
                con,
                run_id,
                finished_at=datetime.now(auth.IST).replace(tzinfo=None),
                errors=1,
                note=str(exc),
            )
            raise SystemExit(str(exc)) from exc

        interval = getattr(args, "interval", "minute") or "minute"
        if interval not in ("minute", "day"):
            raise SystemExit(f"unsupported --interval: {interval}")
        try:
            backfill_days = planner.parse_days_arg(getattr(args, "days", None))
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        if interval == "day":
            backfill_days = planner.BACKFILL_WINDOW_DAYS  # ignored for day estimates
        days_label = "all" if backfill_days is None else str(backfill_days)
        series = universe.select_series(con, interval=interval, market=market_id)
        ranks = rank_mod.load_ranks(con)
        tokens = [c.token for s in series for c in s.contracts]
        fetch_states = catalog.list_fetch_states(con, interval=interval, tokens=tokens)
        now = datetime.now(auth.IST)
        items = planner.build_queue(
            series,
            ranks,
            fetch_states,
            now,
            expiry_filter=expiry_filter,
            underlying_filter=args.underlying,
            limit=args.limit,
            interval=interval,
            backfill_days=backfill_days,
        )

        catalog.finish_run(
            con,
            run_id,
            finished_at=datetime.now(auth.IST).replace(tzinfo=None),
            note=(
                f"market={market_id} interval={interval} days={days_label} "
                f"series={len(items)} contracts_synced={count}"
            ),
        )

        print(
            f"Synced {count} {profile.exchange} contracts "
            f"(market={market_id}, archive {archive_path})"
        )
        print(f"Interval: {interval}" + (f"  --days={days_label}" if interval == "minute" else ""))
        if not ranks:
            print(
                "Note: no ranks yet - run `uv run ohlcv rank` for traded-value ordering "
                "(NFO only until Phase 3+)"
                if market_id != "nfo"
                else "Note: no ranks yet - run `uv run ohlcv rank` for traded-value ordering"
            )
        print()
        print(
            f"{'expiry':<10} {'underlying':<16} {'contracts':>9} "
            f"{'requests':>9} {'est_min':>8} {'traded_value':>14}"
        )
        print("-" * 72)

        by_expiry: dict[date, list] = defaultdict(list)
        for item in items:
            by_expiry[item.expiry].append(item)
            tv = f"{item.traded_value:,.0f}" if item.traded_value is not None else "-"
            print(
                f"{ymd(item.expiry):<10} {item.underlying:<16} {item.contracts:>9} "
                f"{item.pending_requests:>9} {item.estimated_minutes:>8.1f} {tv:>14}"
            )

        print()
        print("Subtotals by expiry:")
        total_req = 0
        total_sec = 0.0
        total_contracts = 0
        for expiry in sorted(by_expiry):
            group = by_expiry[expiry]
            req = sum(i.pending_requests for i in group)
            sec = sum(i.estimated_seconds for i in group)
            contracts = sum(i.contracts for i in group)
            total_req += req
            total_sec += sec
            total_contracts += contracts
            print(
                f"  {ymd(expiry)}: {len(group)} series, {contracts} contracts, "
                f"{req} requests, ~{sec / 60:.1f} min"
            )
        print(
            f"Total: {len(items)} series, {total_contracts} contracts, "
            f"{total_req} requests, ~{total_sec / 60:.1f} min"
        )
        print(f"Catalog: {catalog_path}")
    finally:
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="ohlcv",
        description=(
            "OHLCV tools (probe, sync, rank, queue, daily, fetch, status, show). "
            "Use --market nfo|mcx|cds on sync/queue (default nfo)."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_probe = sub.add_parser(
        "probe",
        help="Measure Kite historical/quote behaviour; write JSON report under data/ohlcv/nfo/_probe/.",
    )
    p_probe.add_argument(
        "--token",
        type=int,
        default=None,
        help="Optional instrument_token override (recorded as role=override).",
    )
    p_probe.add_argument(
        "--expired",
        action="store_true",
        help="Re-test watch_list tokens from the latest probe report (run on/after expiry).",
    )
    p_probe.set_defaults(func=cmd_probe)

    p_sync = sub.add_parser(
        "sync",
        help=(
            "Archive instruments master and upsert contracts into "
            "data/ohlcv/<market>/catalog.duckdb (NFO: all with expiry; MCX/CDS: FUT)."
        ),
    )
    _add_market_arg(p_sync)
    p_sync.set_defaults(func=cmd_sync)

    p_rank = sub.add_parser(
        "rank",
        help="Rank underlyings from final quote snapshot or FUT day candles.",
    )
    p_rank.add_argument(
        "--force",
        action="store_true",
        help="Refresh ranks even if a recent trade_date already exists.",
    )
    p_rank.set_defaults(func=cmd_rank)

    p_queue = sub.add_parser(
        "queue",
        help="Print download queue (expiry asc, traded value desc) with request estimates.",
    )
    _add_market_arg(p_queue)
    p_queue.add_argument(
        "--expiry",
        default=None,
        help="Filter to one expiry (yyyymmdd or YYYY-MM-DD).",
    )
    p_queue.add_argument(
        "--underlying",
        default=None,
        help="Filter to one underlying name (e.g. NIFTY).",
    )
    p_queue.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Print at most N series rows.",
    )
    p_queue.add_argument(
        "--interval",
        choices=("minute", "day"),
        default="minute",
        help="Candle interval for pending-request estimates (default: minute).",
    )
    p_queue.add_argument(
        "-d",
        "--days",
        default="60",
        metavar="N|all",
        help="Minute backfill window for estimates (default: 60). Ignored when --interval day.",
    )
    p_queue.set_defaults(func=cmd_queue)

    p_daily = sub.add_parser(
        "daily",
        help="End-of-day quote() snapshot of all live NFO contracts (run after 15:40 IST).",
    )
    p_daily.add_argument(
        "--dry-run",
        action="store_true",
        help="Print contract/batch estimates without calling quote().",
    )
    p_daily.add_argument(
        "--show",
        nargs="?",
        const="",
        default=None,
        metavar="YYYYMMDD",
        help="Show an existing snapshot (default: latest). Offline; no quote() calls.",
    )
    p_daily.add_argument(
        "--log-file",
        default=None,
        metavar="PATH",
        help="Write run log to PATH (default: data/ohlcv/nfo/logs/daily_YYYYMMDD_HHMMSS.log).",
    )
    p_daily.set_defaults(func=cmd_daily)

    p_fetch = sub.add_parser(
        "fetch",
        help=(
            "Fetch day (~2000d) then minute candles for queued NFO series "
            "(expiry-first, incremental)."
        ),
    )
    p_fetch.add_argument(
        "--expiry",
        default=None,
        help="Filter to one expiry (yyyymmdd or YYYY-MM-DD).",
    )
    p_fetch.add_argument(
        "--underlying",
        default=None,
        help="Filter to one underlying name (e.g. NIFTY).",
    )
    p_fetch.add_argument(
        "-d",
        "--days",
        default="60",
        metavar="N|all",
        help=(
            "Minute backfill window in inclusive calendar days (default: 60), "
            "or 'all' to bound from traded day bars after the day pass."
        ),
    )
    p_fetch.add_argument(
        "--max-series",
        type=int,
        default=None,
        help="Process at most N series then stop (commits progress).",
    )
    p_fetch.add_argument(
        "--max-minutes",
        type=float,
        default=None,
        help="Stop after about M minutes across both passes (finishes current contract).",
    )
    p_fetch.add_argument(
        "--dry-run",
        action="store_true",
        help="Print queue and skip estimates without calling historical_data().",
    )
    p_fetch.add_argument(
        "--log-file",
        default=None,
        metavar="PATH",
        help="Write run log to PATH (default: data/ohlcv/nfo/logs/fetch_YYYYMMDD_HHMMSS.log).",
    )
    p_fetch.set_defaults(func=cmd_fetch)

    p_status = sub.add_parser(
        "status",
        help="Offline progress report: series completion, snapshot gaps, recent runs.",
    )
    p_status.add_argument(
        "--all",
        action="store_true",
        help="Include every series with a series parquet file (not just the live queue).",
    )
    p_status.add_argument(
        "--expiry",
        default=None,
        help="Filter to one expiry (yyyymmdd or YYYY-MM-DD).",
    )
    p_status.add_argument(
        "--underlying",
        default=None,
        help="Filter to one underlying name (e.g. NIFTY or M&M).",
    )
    p_status.add_argument(
        "--interval",
        choices=("minute", "day"),
        default="minute",
        help="Candle interval (default: minute).",
    )
    p_status.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Print at most N series rows.",
    )
    p_status.add_argument(
        "--runs",
        type=int,
        default=5,
        help="How many recent catalog runs to list (default 5).",
    )
    p_status.set_defaults(func=cmd_status)

    p_show = sub.add_parser(
        "show",
        help="Inspect one minute/day series file (offline).",
    )
    p_show.add_argument(
        "underlying",
        help="Underlying name (catalog form, e.g. NIFTY or M&M).",
    )
    p_show.add_argument(
        "expiry",
        help="Expiry yyyymmdd or YYYY-MM-DD.",
    )
    p_show.add_argument(
        "--interval",
        choices=("minute", "day"),
        default="minute",
        help="Candle interval (default: minute).",
    )
    p_show.add_argument(
        "--symbol",
        default=None,
        help="Filter to one tradingsymbol.",
    )
    p_show.add_argument(
        "--token",
        type=int,
        default=None,
        help="Filter to one instrument_token.",
    )
    p_show.add_argument(
        "--tail",
        type=int,
        default=5,
        help="With --symbol/--token, print the last N bars (default 5).",
    )
    p_show.set_defaults(func=cmd_show)

    args = parser.parse_args()
    args.func(args)
