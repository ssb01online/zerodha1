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


def cmd_rank(args: argparse.Namespace) -> None:
    from . import catalog
    from . import rank as rank_mod
    from .client import KiteClient, SessionExpired
    from .paths import CATALOG_PATH

    started = datetime.now(auth.IST)
    try:
        kite = auth.get_kite()
    except (auth.NotLoggedIn, TokenException) as exc:
        _login_exit(exc)

    try:
        con = catalog.connect()
    except catalog.CatalogLocked as exc:
        raise SystemExit(str(exc)) from exc
    try:
        catalog.ensure_schema(con)
        run_id = catalog.start_run(con, "rank", started_at=started.replace(tzinfo=None))
        try:
            count, archive_path = _run_sync(con)
            print(f"Synced {count} NFO contracts (archive {archive_path})")
            client = KiteClient(kite)
            result = rank_mod.refresh_rank(client, con, force=args.force)
        except SessionExpired as exc:
            catalog.finish_run(
                con,
                run_id,
                finished_at=datetime.now(auth.IST).replace(tzinfo=None),
                requests=0,
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
            requests=client.counters.requests,
            retries=client.counters.retries,
            errors=client.counters.errors + len(result.errors),
            note=(
                f"skipped={result.skipped} upserted={result.upserted} "
                f"trade_date={result.trade_date}"
            ),
        )

        if result.skipped:
            print(f"Rank fresh (trade_date={result.trade_date}); skipped Kite calls (use --force)")
        else:
            print(
                f"Ranked {result.upserted} underlyings "
                f"(trade_date={result.trade_date}, "
                f"requests={client.counters.requests}, retries={client.counters.retries})"
            )
        if result.errors:
            print(f"Errors ({len(result.errors)}):", file=sys.stderr)
            for err in result.errors[:20]:
                print(f"  {err}", file=sys.stderr)

        ranks = sorted(
            rank_mod.load_ranks(con).values(),
            key=lambda r: r.traded_value,
            reverse=True,
        )
        print()
        print(f"{'#':<4} {'underlying':<16} {'traded_value':>16} {'volume':>12} {'close':>10}")
        print("-" * 64)
        for i, r in enumerate(ranks[:20], start=1):
            print(
                f"{i:<4} {r.underlying:<16} {r.traded_value:>16,.0f} "
                f"{r.volume:>12,} {r.close:>10.2f}"
            )
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
        description="NFO OHLCV tools (probe, sync, rank, queue).",
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
        help="Rank underlyings by nearest-FUT day traded value (needs Kite login).",
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

    args = parser.parse_args()
    args.func(args)
