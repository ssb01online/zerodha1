"""CLI entry: uv run preopen ..."""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime

from kiteconnect.exceptions import TokenException

from kitetools import auth

from .capture import (
    DEFAULT_CAPTURE_AT,
    CaptureRejected,
    CaptureResult,
    parse_at,
)
from .client import RetryExhausted, SessionExpired


def _login_exit(exc: BaseException) -> None:
    raise SystemExit(f"{exc}\nRun: uv run kitetools login") from exc


def _parse_ymd(text: str) -> date:
    text = text.strip()
    if len(text) == 8 and text.isdigit():
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    return date.fromisoformat(text)


def _fmt_seconds(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def cmd_capture(args: argparse.Namespace) -> None:
    from . import capture as capture_mod
    from .client import KiteClient

    try:
        at = parse_at(args.at) if args.at else DEFAULT_CAPTURE_AT
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    skip_wait = bool(args.force)
    if args.dry_run:
        from .universe import list_equity

        equities = list_equity()
        estimate = capture_mod.estimate_dry_run(
            equities,
            at=at,
            skip_wait=skip_wait,
        )
        print(
            f"equities={estimate.equities} batches={estimate.batches} "
            f"quote_est={_fmt_seconds(estimate.estimated_seconds)}",
            file=sys.stderr,
        )
        if estimate.wait_until is not None:
            print(
                f"wait_until={estimate.wait_until:%Y-%m-%d %H:%M:%S} IST "
                f"({_fmt_seconds(estimate.wait_seconds)})",
                file=sys.stderr,
            )
        else:
            print("wait=skip (already past target or --force)", file=sys.stderr)
        return

    def on_wait(remaining: float, target: datetime) -> None:
        print(
            f"waiting until {target:%Y-%m-%d %H:%M:%S} IST "
            f"({_fmt_seconds(remaining)})…",
            file=sys.stderr,
        )

    def on_batch(batch_i: int, batch_total: int, done: int, total: int) -> None:
        print(
            f"quote batch {batch_i}/{batch_total} ({done}/{total})",
            file=sys.stderr,
        )

    try:
        kite = auth.get_kite()
        client = KiteClient(kite)
        result = capture_mod.run_capture(
            client,
            at=at,
            skip_wait=skip_wait,
            on_wait=on_wait,
            on_batch=on_batch,
        )
    except (auth.NotLoggedIn, TokenException, SessionExpired) as exc:
        _login_exit(exc)
    except CaptureRejected as exc:
        raise SystemExit(str(exc)) from exc
    except RetryExhausted as exc:
        raise SystemExit(str(exc)) from exc

    assert isinstance(result, CaptureResult)
    print(
        f"wrote {result.path} rows={result.equities} traded={result.traded_rows} "
        f"missing={result.missing} "
        f"traded_value={result.total_traded_value:,.0f}",
        file=sys.stderr,
    )


def cmd_show(args: argparse.Namespace) -> None:
    from . import read as read_mod

    trade_date = _parse_ymd(args.date) if args.date else None
    try:
        payload = read_mod.show_summary(trade_date, limit=args.limit)
    except FileNotFoundError as exc:
        raise SystemExit(str(exc)) from exc

    print(
        f"{payload['ymd']} rows={payload['day_rows']} "
        f"traded={payload['day_traded_rows']} "
        f"total_traded_value={payload['day_total_traded_value']:,.0f}",
        file=sys.stderr,
    )
    print(
        f"{'exch':4} {'symbol':12} {'volume':>12} {'avg':>10} {'traded_value':>14}"
    )
    for item in payload["items"]:
        print(
            f"{item['exchange']:4} {item['tradingsymbol'][:12]:12} "
            f"{int(item['volume']):>12,} "
            f"{float(item['average_price'] or 0):>10.2f} "
            f"{float(item['traded_value']):>14,.0f}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="preopen",
        description="NSE/BSE equity pre-open quote snapshots",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    cap = sub.add_parser(
        "capture",
        help="Wait until IST time then quote()-sweep all NSE/BSE EQ (batch 500).",
    )
    cap.add_argument(
        "--at",
        default="09:08:30",
        help="IST clock time to start the sweep (default: 09:08:30).",
    )
    cap.add_argument(
        "--force",
        action="store_true",
        help="Skip waiting for --at (run immediately).",
    )
    cap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print equity/batch/wait estimate; no quote calls.",
    )
    cap.set_defaults(func=cmd_capture)

    show = sub.add_parser("show", help="Summarize a stored pre-open snapshot.")
    show.add_argument(
        "date",
        nargs="?",
        default=None,
        help="Trade date yyyymmdd or YYYY-MM-DD (default: latest).",
    )
    show.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Top-N by traded_value (default: 10).",
    )
    show.set_defaults(func=cmd_show)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
