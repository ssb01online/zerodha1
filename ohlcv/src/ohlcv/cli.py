"""CLI entry: uv run ohlcv ..."""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date, datetime

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


def cmd_probe(args: argparse.Namespace) -> None:
    from . import probe

    try:
        path = probe.run_probe(token=args.token, expired=args.expired)
    except (auth.NotLoggedIn, TokenException) as exc:
        _login_exit(exc)
    print(f"Wrote {path}", file=sys.stderr)


def _run_sync(con) -> tuple[int, object]:
    from . import archive as archive_mod
    from . import universe

    from instruments import ensure_cache

    ensure_cache()
    archive_path = archive_mod.ensure_archive()
    count = universe.sync_contracts(con)
    return count, archive_path


def cmd_sync(args: argparse.Namespace) -> None:
    from . import catalog
    from .paths import CATALOG_PATH

    started = datetime.now(auth.IST)
    try:
        con = catalog.connect()
    except catalog.CatalogLocked as exc:
        raise SystemExit(str(exc)) from exc
    try:
        catalog.ensure_schema(con)
        run_id = catalog.start_run(con, "sync", started_at=started.replace(tzinfo=None))
        try:
            count, archive_path = _run_sync(con)
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
            note=f"contracts={count} archive={archive_path}",
        )
        print(f"Synced {count} NFO contracts")
        print(f"Archive: {archive_path}")
        print(f"Catalog: {CATALOG_PATH}")
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

    if args.show is not None:
        _cmd_daily_show(args)
        return

    started = datetime.now(auth.IST)
    try:
        con = catalog.connect()
    except catalog.CatalogLocked as exc:
        raise SystemExit(str(exc)) from exc

    client = None
    try:
        catalog.ensure_schema(con)
        run_id = catalog.start_run(con, "daily", started_at=started.replace(tzinfo=None))
        try:
            count, archive_path = _run_sync(con)
            print(f"Synced {count} NFO contracts (archive {archive_path})")
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
            result = snapshot.take_snapshot(client, contracts)
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
    finally:
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


def cmd_fetch(args: argparse.Namespace) -> None:
    from . import catalog
    from . import fetch as fetch_mod
    from . import planner
    from . import rank as rank_mod
    from . import universe
    from .client import KiteClient, SessionExpired
    from .paths import CATALOG_PATH, MINUTE_DIR, ymd as paths_ymd

    started = datetime.now(auth.IST)
    expiry_filter = _parse_ymd(args.expiry) if args.expiry else None
    try:
        con = catalog.connect()
    except catalog.CatalogLocked as exc:
        raise SystemExit(str(exc)) from exc

    client = None
    try:
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

        print(f"Synced {count} NFO contracts (archive {archive_path})")
        series = universe.select_series(con)
        ranks = rank_mod.load_ranks(con)
        tokens = [c.token for s in series for c in s.contracts]
        fetch_states = catalog.list_fetch_states(con, interval="minute", tokens=tokens)
        now = datetime.now(auth.IST)
        items = planner.build_queue(
            series,
            ranks,
            fetch_states,
            now,
            expiry_filter=expiry_filter,
            underlying_filter=args.underlying,
            limit=args.max_series,
        )
        series_by_key = {(s.underlying, s.expiry): s for s in series}
        queue_series = [
            series_by_key[(item.underlying, item.expiry)]
            for item in items
            if (item.underlying, item.expiry) in series_by_key
        ]

        if args.dry_run:
            skip_cache: dict = {}
            print()
            print(
                f"{'expiry':<10} {'underlying':<16} {'contracts':>9} "
                f"{'requests':>9} {'skip~':>6} {'est_min':>8}"
            )
            print("-" * 68)
            total_req = 0
            total_sec = 0.0
            total_skip = 0
            for item, s in zip(items, queue_series):
                skip_n = fetch_mod.count_skippable(
                    con, s, fetch_states, now=now, untraded_cache=skip_cache
                )
                # Approximate: each skip removes one pending request.
                adj_req = max(0, item.pending_requests - skip_n)
                adj_sec = adj_req * planner.request_seconds()
                total_req += adj_req
                total_sec += adj_sec
                total_skip += skip_n
                print(
                    f"{paths_ymd(item.expiry):<10} {item.underlying:<16} {item.contracts:>9} "
                    f"{adj_req:>9} {skip_n:>6} {adj_sec / 60:>8.1f}"
                )
            catalog.finish_run(
                con,
                run_id,
                finished_at=datetime.now(auth.IST).replace(tzinfo=None),
                note=(
                    f"dry_run series={len(items)} requests~={total_req} "
                    f"skip~={total_skip}"
                ),
            )
            print()
            print(
                f"Dry run: {len(items)} series, ~{total_req} requests "
                f"(~{total_skip} untraded skips), ~{total_sec / 60:.1f} min"
            )
            print(f"Catalog: {CATALOG_PATH}")
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

        def _progress(idx: int, total: int, result: fetch_mod.SeriesResult) -> None:
            flag = " LOCKED" if result.locked else ""
            print(
                f"[{idx}/{total}] {paths_ymd(result.expiry)} {result.underlying} "
                f"{result.contracts} contracts fetched={result.fetched} "
                f"skipped={result.skipped} rows={result.rows:,} "
                f"{result.elapsed_s:.1f}s{flag}",
                file=sys.stderr,
            )

        try:
            run = fetch_mod.run_fetch(
                client,
                con,
                queue_series,
                fetch_states,
                now=now,
                max_series=args.max_series,
                max_minutes=args.max_minutes,
                on_series=_progress,
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

        finished = datetime.now(auth.IST).replace(tzinfo=None)
        note_parts = [
            f"series_done={run.series_done}",
            f"fetched={run.contracts_fetched}",
            f"skipped={run.contracts_skipped}",
            f"rows={run.rows_written}",
            f"errors={run.errors}",
            f"gave_up={run.gave_up}",
        ]
        if run.locked:
            note_parts.append(
                "locked="
                + ",".join(f"{u}:{paths_ymd(e)}" for u, e in run.locked)
            )
        if run.stopped_reason:
            note_parts.append(f"stopped={run.stopped_reason}")
        catalog.finish_run(
            con,
            run_id,
            finished_at=finished,
            requests=client.counters.requests,
            retries=client.counters.retries,
            errors=client.counters.errors + run.errors,
            note=" ".join(note_parts),
        )

        print()
        print(
            f"Done: {run.series_done} series, fetched={run.contracts_fetched}, "
            f"skipped={run.contracts_skipped}, rows={run.rows_written:,}, "
            f"errors={run.errors}, gave_up={run.gave_up}"
        )
        if run.locked:
            print(f"Locked files ({len(run.locked)}): close notebooks and re-run")
            for u, e in run.locked:
                print(f"  {u} {paths_ymd(e)}")
        if run.stopped_reason:
            print(f"Stopped: {run.stopped_reason}")
        print(f"Minute dir: {MINUTE_DIR}")
        print(f"Catalog: {CATALOG_PATH}")
    finally:
        con.close()


def cmd_queue(args: argparse.Namespace) -> None:
    from . import catalog
    from . import planner
    from . import rank as rank_mod
    from . import universe
    from .paths import CATALOG_PATH

    started = datetime.now(auth.IST)
    expiry_filter = _parse_ymd(args.expiry) if args.expiry else None
    try:
        con = catalog.connect()
    except catalog.CatalogLocked as exc:
        raise SystemExit(str(exc)) from exc
    try:
        catalog.ensure_schema(con)
        run_id = catalog.start_run(con, "queue", started_at=started.replace(tzinfo=None))
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

        series = universe.select_series(con)
        ranks = rank_mod.load_ranks(con)
        tokens = [c.token for s in series for c in s.contracts]
        fetch_states = catalog.list_fetch_states(con, interval="minute", tokens=tokens)
        now = datetime.now(auth.IST)
        items = planner.build_queue(
            series,
            ranks,
            fetch_states,
            now,
            expiry_filter=expiry_filter,
            underlying_filter=args.underlying,
            limit=args.limit,
        )

        catalog.finish_run(
            con,
            run_id,
            finished_at=datetime.now(auth.IST).replace(tzinfo=None),
            note=f"series={len(items)} contracts_synced={count}",
        )

        print(f"Synced {count} NFO contracts (archive {archive_path})")
        if not ranks:
            print("Note: no ranks yet - run `uv run ohlcv rank` for traded-value ordering")
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
        print(f"Catalog: {CATALOG_PATH}")
    finally:
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="ohlcv",
        description="NFO OHLCV tools (probe, sync, rank, queue, daily, fetch).",
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
        help="Archive instruments master and upsert NFO contracts into catalog.duckdb.",
    )
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
    p_daily.set_defaults(func=cmd_daily)

    p_fetch = sub.add_parser(
        "fetch",
        help="Fetch minute candles for queued NFO series (expiry-first, incremental).",
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
        "--max-series",
        type=int,
        default=None,
        help="Process at most N series then stop (commits progress).",
    )
    p_fetch.add_argument(
        "--max-minutes",
        type=float,
        default=None,
        help="Stop after about M minutes (finishes current contract, then commits).",
    )
    p_fetch.add_argument(
        "--dry-run",
        action="store_true",
        help="Print queue and skip estimates without calling historical_data().",
    )
    p_fetch.set_defaults(func=cmd_fetch)

    args = parser.parse_args()
    args.func(args)
