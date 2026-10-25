import argparse

from . import samples


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
    folder = samples.resolve_sample_dir(args.folder)
    samples.summarize(folder)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="portfolio",
        description="Read-only portfolio samples and (later) snapshots/UI.",
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

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
