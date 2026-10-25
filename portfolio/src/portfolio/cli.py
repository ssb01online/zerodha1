import argparse
from pathlib import Path

from . import capture as capture_mod
from . import samples
from . import store
from .fetch import TokenInvalid
from .samples import resolve_sample_dir

_DB_LOCK_HINT = (
    "Database is in use by `portfolio serve`; capture from the UI instead "
    "(POST /api/portfolio/capture), or stop the server first."
)


def cmd_sample(args: argparse.Namespace) -> None:
    folder = samples.capture_sample()
    meta = samples.load_sample(folder)["meta"]
    counts = meta.get("counts") or {}
    print(f"Saved sample to {folder}")
    print(
        f"holdings={counts.get('holdings')} "
        f"positions_net={counts.get('positions_net')} "
        f"positions_day={counts.get('positions_day')} "
        f"mf_holdings={counts.get('mf_holdings')} "
        f"user={meta.get('user_id')}"
    )


def cmd_summarize(args: argparse.Namespace) -> None:
    folder = resolve_sample_dir(args.folder)
    samples.summarize(folder)


def cmd_capture(args: argparse.Namespace) -> None:
    db_path = Path(args.db) if args.db else None
    try:
        if args.from_sample is not None:
            # --from-sample with no value (const="") means latest sample.
            folder_arg = args.from_sample or None
            folder = resolve_sample_dir(folder_arg)
            result = capture_mod.capture_from_sample(folder, db_path)
            print(f"Source: sample {folder}")
        else:
            result = capture_mod.capture_live(db_path)
            print("Source: live")
    except TokenInvalid as exc:
        raise SystemExit(str(exc)) from exc
    except Exception as exc:
        msg = str(exc).lower()
        if "lock" in msg or "conflicting" in msg or "already open" in msg:
            raise SystemExit(_DB_LOCK_HINT) from exc
        raise

    if result.saved:
        print(f"Saved snapshot id={result.snapshot_id} hash={result.fingerprint_hash[:8]}")
        print(f"Diff: {result.diff.short_summary()}")
    else:
        print(
            f"No change (existing id={result.snapshot_id} hash={result.fingerprint_hash[:8]})"
        )


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    from .api import create_app

    host = args.host
    port = args.port
    db_path = Path(args.db) if args.db else None
    if host not in {"127.0.0.1", "localhost", "::1"}:
        print(
            f"WARNING: binding to {host!r} exposes the portfolio API (and your "
            "session-backed data) beyond this machine. Prefer 127.0.0.1."
        )
    app = create_app(db_path=db_path)
    print(f"Serving portfolio API on http://{host}:{port}  (health: /api/health)")
    uvicorn.run(app, host=host, port=port, log_level="info")


def cmd_snapshots(args: argparse.Namespace) -> None:
    db_path = Path(args.db) if args.db else None
    con = store.connect(db_path)
    try:
        store.ensure_schema(con)
        rows = store.list_snapshots(con, limit=args.limit)
    finally:
        con.close()
    if not rows:
        print("No snapshots.")
        return
    for s in rows:
        counts = s.counts
        print(
            f"id={s.id} observed_at={s.observed_at} source={s.source} "
            f"hash={s.fingerprint_hash[:8]} "
            f"h={counts.get('holdings')} net={counts.get('positions_net')} "
            f"day={counts.get('positions_day')} mf={counts.get('mf_holdings')}"
        )


def cmd_show(args: argparse.Namespace) -> None:
    db_path = Path(args.db) if args.db else None
    con = store.connect(db_path)
    try:
        store.ensure_schema(con)
        record = store.get_snapshot(con, args.id)
    except KeyError as exc:
        raise SystemExit(str(exc)) from exc
    finally:
        con.close()

    meta = record.meta
    counts = meta.counts
    print(f"Snapshot id={meta.id}")
    print(f"observed_at={meta.observed_at}  effective_date={meta.effective_date}")
    print(f"source={meta.source}  hash={meta.fingerprint_hash}")
    print(
        f"counts: holdings={counts.get('holdings')} "
        f"positions_net={counts.get('positions_net')} "
        f"positions_day={counts.get('positions_day')} "
        f"mf_holdings={counts.get('mf_holdings')}"
    )
    print("composition:")
    for key, qty in sorted(record.composition.items()):
        print(f"  {key} = {qty}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="portfolio",
        description="Read-only portfolio samples and composition snapshots.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_sample = sub.add_parser(
        "sample",
        help="Fetch holdings, positions, and mf_holdings; save under data/samples/.",
    )
    p_sample.set_defaults(func=cmd_sample)

    p_sum = sub.add_parser(
        "summarize",
        help="Print quantity/field patterns from a saved sample (default: latest).",
    )
    p_sum.add_argument(
        "folder",
        nargs="?",
        default=None,
        help="Sample folder path or timestamp name under data/samples/ (default: latest).",
    )
    p_sum.set_defaults(func=cmd_summarize)

    p_cap = sub.add_parser(
        "capture",
        help="Capture a composition snapshot (live, or from a sample). Writes only on change.",
    )
    p_cap.add_argument(
        "--from-sample",
        nargs="?",
        const="",
        default=None,
        help="Use a sample folder (or latest if flag given with no value) instead of live Kite.",
    )
    p_cap.add_argument(
        "--db",
        default=None,
        help=f"DuckDB path (default: {store.DEFAULT_DB_PATH}).",
    )
    p_cap.set_defaults(func=cmd_capture)

    p_list = sub.add_parser("snapshots", help="List stored composition snapshots.")
    p_list.add_argument("--limit", type=int, default=50, help="Max rows (default 50).")
    p_list.add_argument("--db", default=None, help=f"DuckDB path (default: {store.DEFAULT_DB_PATH}).")
    p_list.set_defaults(func=cmd_snapshots)

    p_show = sub.add_parser("show", help="Show one snapshot's summary and composition.")
    p_show.add_argument("id", type=int, help="Snapshot id.")
    p_show.add_argument("--db", default=None, help=f"DuckDB path (default: {store.DEFAULT_DB_PATH}).")
    p_show.set_defaults(func=cmd_show)

    p_serve = sub.add_parser(
        "serve",
        help="Run the local FastAPI portfolio server (JSON API for the UI).",
    )
    p_serve.add_argument("--host", default="127.0.0.1", help="Bind address (default 127.0.0.1).")
    p_serve.add_argument("--port", type=int, default=8765, help="Port (default 8765).")
    p_serve.add_argument("--db", default=None, help=f"DuckDB path (default: {store.DEFAULT_DB_PATH}).")
    p_serve.set_defaults(func=cmd_serve)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
