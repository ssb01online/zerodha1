"""Fetch and save raw Kite portfolio responses; summarize quantity patterns."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import kiteconnect
from kiteconnect.exceptions import TokenException

from kitetools import auth
from kitetools.auth import IST
from kitetools.config import DATA_DIR

SAMPLES_DIR = DATA_DIR / "samples"
TOKEN_HINT = "Token is invalid or expired. Run: uv run kitetools login"


def _now_ist() -> datetime:
    return datetime.now(IST)


def _dump(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def fetch_live() -> tuple[list, dict, list, dict]:
    """Return holdings, positions, mf_holdings, and a small profile dict."""
    try:
        kite = auth.get_kite()
        profile = kite.profile()
        holdings = kite.holdings()
        positions = kite.positions()
        mf_holdings = kite.mf_holdings()
    except (auth.NotLoggedIn, TokenException) as exc:
        raise SystemExit(f"{TOKEN_HINT}\n({exc})") from exc
    return holdings, positions, mf_holdings, {
        "user_id": profile.get("user_id"),
        "user_name": profile.get("user_name"),
    }


def save_sample(
    holdings: list,
    positions: dict,
    mf_holdings: list,
    profile: dict,
    *,
    fetched_at: datetime | None = None,
) -> Path:
    fetched_at = fetched_at or _now_ist()
    folder = SAMPLES_DIR / fetched_at.strftime("%Y%m%d-%H%M%S")
    folder.mkdir(parents=True, exist_ok=False)

    _dump(folder / "holdings.json", holdings)
    _dump(folder / "positions.json", positions)
    _dump(folder / "mf_holdings.json", mf_holdings)

    net = positions.get("net") or []
    day = positions.get("day") or []
    try:
        from kiteconnect.__version__ import __version__ as kite_version
    except Exception:
        kite_version = getattr(kiteconnect, "__version__", "unknown")
        if not isinstance(kite_version, str):
            kite_version = str(kite_version)

    meta = {
        "fetched_at": fetched_at.isoformat(),
        "kiteconnect_version": kite_version,
        "user_id": profile.get("user_id"),
        "user_name": profile.get("user_name"),
        "counts": {
            "holdings": len(holdings),
            "positions_net": len(net),
            "positions_day": len(day),
            "mf_holdings": len(mf_holdings),
        },
    }
    _dump(folder / "meta.json", meta)
    return folder


def capture_sample() -> Path:
    holdings, positions, mf_holdings, profile = fetch_live()
    return save_sample(holdings, positions, mf_holdings, profile)


def latest_sample_dir() -> Path:
    if not SAMPLES_DIR.is_dir():
        raise SystemExit(f"No samples under {SAMPLES_DIR}. Run: uv run portfolio sample")
    dirs = sorted((p for p in SAMPLES_DIR.iterdir() if p.is_dir()), reverse=True)
    if not dirs:
        raise SystemExit(f"No samples under {SAMPLES_DIR}. Run: uv run portfolio sample")
    return dirs[0]


def resolve_sample_dir(folder: str | None) -> Path:
    if folder is None:
        return latest_sample_dir()
    path = Path(folder)
    if not path.is_absolute():
        # Allow bare timestamp folder name or relative path.
        candidate = SAMPLES_DIR / folder
        if candidate.is_dir():
            path = candidate
    if not path.is_dir():
        raise SystemExit(f"Sample folder not found: {folder}")
    return path


def load_sample(folder: Path) -> dict[str, Any]:
    required = ("holdings.json", "positions.json", "mf_holdings.json", "meta.json")
    missing = [name for name in required if not (folder / name).exists()]
    if missing:
        raise SystemExit(f"{folder} is missing: {', '.join(missing)}")
    return {
        "folder": folder,
        "holdings": _load(folder / "holdings.json"),
        "positions": _load(folder / "positions.json"),
        "mf_holdings": _load(folder / "mf_holdings.json"),
        "meta": _load(folder / "meta.json"),
    }


def _field_set(rows: list[dict]) -> list[str]:
    keys: set[str] = set()
    for row in rows:
        keys.update(row.keys())
    return sorted(keys)


def _nested_fields(rows: list[dict], nest: str) -> list[str]:
    keys: set[str] = set()
    for row in rows:
        nested = row.get(nest)
        if isinstance(nested, dict):
            keys.update(nested.keys())
    return sorted(keys)


def _qty_precision(value: Any) -> int | None:
    if value is None:
        return None
    text = f"{value}"
    if "e" in text.lower():
        text = f"{float(value):.12f}".rstrip("0")
    if "." not in text:
        return 0
    return len(text.split(".", 1)[1])


def _print_section(title: str) -> None:
    print()
    print(f"== {title} ==")


def summarize(folder: Path) -> None:
    sample = load_sample(folder)
    holdings: list[dict] = sample["holdings"]
    positions: dict = sample["positions"]
    mf_holdings: list[dict] = sample["mf_holdings"]
    meta: dict = sample["meta"]
    net: list[dict] = positions.get("net") or []
    day: list[dict] = positions.get("day") or []

    print(f"Sample: {folder}")
    print(f"Fetched: {meta.get('fetched_at')}  user={meta.get('user_id')}  kiteconnect={meta.get('kiteconnect_version')}")
    print(f"Counts: holdings={len(holdings)} net={len(net)} day={len(day)} mf={len(mf_holdings)}")

    # --- Holdings ---
    _print_section("Holdings")
    print(f"fields: {', '.join(_field_set(holdings)) or '(none)'}")
    mtf_fields = _nested_fields(holdings, "mtf")
    if mtf_fields:
        print(f"mtf fields: {', '.join(mtf_fields)}")

    flags = (
        ("t1_quantity", "t1_quantity"),
        ("used_quantity", "used_quantity"),
        ("collateral_quantity", "collateral_quantity"),
        ("short_quantity", "short_quantity"),
    )
    for label, key in flags:
        hits = [r for r in holdings if (r.get(key) or 0) != 0]
        print(f"non-zero {label}: {len(hits)}")
        for r in hits[:10]:
            print(
                f"  {r.get('exchange')}:{r.get('tradingsymbol')} "
                f"qty={r.get('quantity')} t1={r.get('t1_quantity')} "
                f"used={r.get('used_quantity')} realised={r.get('realised_quantity')} "
                f"opening={r.get('opening_quantity')} coll={r.get('collateral_quantity')}"
            )
        if len(hits) > 10:
            print(f"  ... {len(hits) - 10} more")

    mtf_hits = [r for r in holdings if ((r.get("mtf") or {}).get("quantity") or 0) != 0]
    print(f"non-zero mtf.quantity: {len(mtf_hits)}")
    for r in mtf_hits[:10]:
        mtf = r.get("mtf") or {}
        print(f"  {r.get('exchange')}:{r.get('tradingsymbol')} mtf.qty={mtf.get('quantity')} mtf.avg={mtf.get('average_price')}")

    def _owned(r: dict) -> int:
        return (r.get("quantity") or 0) + (r.get("t1_quantity") or 0) + (r.get("collateral_quantity") or 0)

    qty_eq = sum(1 for r in holdings if (r.get("quantity") or 0) + (r.get("t1_quantity") or 0) == (r.get("opening_quantity") or 0))
    owned_eq = sum(1 for r in holdings if _owned(r) == (r.get("opening_quantity") or 0))
    print(f"quantity + t1_quantity == opening_quantity: {qty_eq}/{len(holdings)}")
    print(f"quantity + t1_quantity + collateral_quantity == opening_quantity: {owned_eq}/{len(holdings)}")

    mismatches = [r for r in holdings if _owned(r) != (r.get("opening_quantity") or 0)]
    for r in mismatches[:10]:
        print(
            f"  mismatch {r.get('exchange')}:{r.get('tradingsymbol')} "
            f"qty={r.get('quantity')} t1={r.get('t1_quantity')} coll={r.get('collateral_quantity')} "
            f"opening={r.get('opening_quantity')} used={r.get('used_quantity')} realised={r.get('realised_quantity')}"
        )

    las_hits = [r for r in holdings if (r.get("las_quantity") or 0) != 0]
    print(f"non-zero las_quantity: {len(las_hits)}")

    isin_exchanges: dict[str, set[str]] = defaultdict(set)
    for r in holdings:
        isin = r.get("isin")
        if isin:
            isin_exchanges[isin].add(r.get("exchange") or "?")
    dup_isins = {isin: sorted(exs) for isin, exs in isin_exchanges.items() if len(exs) > 1}
    print(f"ISINs on multiple exchanges: {len(dup_isins)}")
    for isin, exs in list(dup_isins.items())[:10]:
        rows = [r for r in holdings if r.get("isin") == isin]
        detail = ", ".join(f"{r.get('exchange')}:{r.get('tradingsymbol')} qty={r.get('quantity')}" for r in rows)
        print(f"  {isin}: {exs} -> {detail}")

    # --- Positions ---
    _print_section("Positions (net)")
    print(f"fields: {', '.join(_field_set(net)) or '(none)'}")
    by_ex_prod: Counter[tuple[str, str]] = Counter(
        (r.get("exchange") or "?", r.get("product") or "?") for r in net
    )
    for (ex, prod), n in sorted(by_ex_prod.items()):
        print(f"  {ex}/{prod}: {n}")

    zero_qty = [r for r in net if (r.get("quantity") or 0) == 0]
    print(f"net quantity==0: {len(zero_qty)}")
    overnight = [r for r in net if (r.get("overnight_quantity") or 0) != 0]
    print(f"net overnight_quantity!=0: {len(overnight)}")
    for r in overnight[:10]:
        print(
            f"  {r.get('exchange')}:{r.get('tradingsymbol')} product={r.get('product')} "
            f"qty={r.get('quantity')} overnight={r.get('overnight_quantity')} mult={r.get('multiplier')}"
        )

    cnc = [r for r in net if (r.get("product") or "").upper() == "CNC"]
    print(f"net CNC rows: {len(cnc)}")
    for r in cnc[:10]:
        print(
            f"  {r.get('exchange')}:{r.get('tradingsymbol')} "
            f"qty={r.get('quantity')} overnight={r.get('overnight_quantity')} avg={r.get('average_price')}"
        )

    multipliers = sorted({r.get("multiplier") for r in net})
    print(f"multipliers seen: {multipliers}")

    _print_section("Positions (day)")
    print(f"fields: {', '.join(_field_set(day)) or '(none)'}")
    by_ex_prod_day: Counter[tuple[str, str]] = Counter(
        (r.get("exchange") or "?", r.get("product") or "?") for r in day
    )
    for (ex, prod), n in sorted(by_ex_prod_day.items()):
        print(f"  {ex}/{prod}: {n}")
    day_zero = [r for r in day if (r.get("quantity") or 0) == 0]
    print(f"day quantity==0: {len(day_zero)}")
    day_nonzero = [r for r in day if (r.get("quantity") or 0) != 0]
    print(f"day quantity!=0: {len(day_nonzero)}")
    for r in day_nonzero[:10]:
        print(
            f"  {r.get('exchange')}:{r.get('tradingsymbol')} product={r.get('product')} "
            f"qty={r.get('quantity')} buy={r.get('day_buy_quantity')} sell={r.get('day_sell_quantity')}"
        )

    # --- MF ---
    _print_section("MF holdings")
    print(f"fields: {', '.join(_field_set(mf_holdings)) or '(none)'}")
    folio_present = sum(1 for r in mf_holdings if r.get("folio"))
    print(f"rows with folio: {folio_present}/{len(mf_holdings)}")

    tradingsymbol_is_isin = 0
    for r in mf_holdings:
        ts = r.get("tradingsymbol") or ""
        isin = r.get("isin") or ""
        if ts and (ts == isin or (len(ts) == 12 and ts.startswith("INF"))):
            tradingsymbol_is_isin += 1
    print(f"tradingsymbol looks like ISIN: {tradingsymbol_is_isin}/{len(mf_holdings)}")

    precisions = [_qty_precision(r.get("quantity")) for r in mf_holdings]
    precisions = [p for p in precisions if p is not None]
    if precisions:
        print(f"quantity decimal precision: min={min(precisions)} max={max(precisions)}")
    else:
        print("quantity decimal precision: (no rows)")

    interesting = (
        "quantity",
        "average_price",
        "last_price",
        "pnl",
        "pledged_quantity",
    )
    for key in interesting:
        if any(key in r for r in mf_holdings):
            nonzero = sum(1 for r in mf_holdings if (r.get(key) or 0) != 0)
            print(f"non-zero {key}: {nonzero}/{len(mf_holdings)}")

    extra_nonzero: dict[str, int] = defaultdict(int)
    skip = {"tradingsymbol", "folio", "fund", "isin", "quantity", "average_price", "last_price", "pnl", "instrument_token"}
    for r in mf_holdings:
        for k, v in r.items():
            if k in skip:
                continue
            if isinstance(v, (int, float)) and v != 0:
                extra_nonzero[k] += 1
            elif isinstance(v, str) and v.strip():
                extra_nonzero[k] += 1
    if extra_nonzero:
        print("other non-empty/non-zero fields:")
        for k, n in sorted(extra_nonzero.items()):
            print(f"  {k}: {n}")

    for r in mf_holdings[:15]:
        print(
            f"  folio={r.get('folio')} isin={r.get('isin')} ts={r.get('tradingsymbol')} "
            f"qty={r.get('quantity')} avg={r.get('average_price')} fund={r.get('fund')}"
        )
    if len(mf_holdings) > 15:
        print(f"  ... {len(mf_holdings) - 15} more")

    # --- Key uniqueness ---
    _print_section("Key uniqueness")
    holding_keys = [(r.get("isin") or f"NOISIN:{r.get('exchange')}:{r.get('tradingsymbol')}") for r in holdings]
    holding_dup = [k for k, n in Counter(holding_keys).items() if n > 1]
    print(f"holdings ISIN keys unique: {not holding_dup} (dups={len(holding_dup)})")
    for k in holding_dup[:10]:
        print(f"  dup ISIN key: {k}")

    # Also report exchange:tradingsymbol uniqueness for holdings
    ht_keys = [f"{r.get('exchange')}:{r.get('tradingsymbol')}" for r in holdings]
    ht_dup = [k for k, n in Counter(ht_keys).items() if n > 1]
    print(f"holdings exchange:tradingsymbol unique: {not ht_dup} (dups={len(ht_dup)})")

    net_keys = [f"{r.get('exchange')}:{r.get('tradingsymbol')}" for r in net]
    # Same symbol can appear under different products — include product in key check
    net_prod_keys = [f"{r.get('exchange')}:{r.get('tradingsymbol')}:{r.get('product')}" for r in net]
    print(f"positions net exchange:tradingsymbol unique: {len(net_keys) == len(set(net_keys))} (n={len(net_keys)})")
    print(
        f"positions net exchange:tradingsymbol:product unique: "
        f"{len(net_prod_keys) == len(set(net_prod_keys))} (n={len(net_prod_keys)})"
    )
    net_sym_dups = [k for k, n in Counter(net_keys).items() if n > 1]
    for k in net_sym_dups[:10]:
        rows = [r for r in net if f"{r.get('exchange')}:{r.get('tradingsymbol')}" == k]
        products = ", ".join(f"{r.get('product')} qty={r.get('quantity')}" for r in rows)
        print(f"  multi-product {k}: {products}")

    mf_keys = [f"{r.get('isin') or r.get('tradingsymbol')}:{r.get('folio')}" for r in mf_holdings]
    mf_dup = [k for k, n in Counter(mf_keys).items() if n > 1]
    print(f"mf isin:folio unique: {not mf_dup} (dups={len(mf_dup)})")
    for k in mf_dup[:10]:
        print(f"  dup mf key: {k}")
