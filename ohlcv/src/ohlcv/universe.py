"""Series universe: sync contracts and select active/unfinished series per market."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Iterable

import duckdb

from instruments import ensure_cache, iter_instruments
from kitetools import auth

from .catalog import (
    ContractRow,
    FetchState,
    list_contracts,
    list_fetch_states,
    upsert_contracts,
)
from .markets import (
    FUT_TYPES,
    OPT_TYPES,
    MarketProfile,
    NFO,
    get_market,
)

NEAREST_EXPIRIES = 3
# NFO alias — existing imports (planner/snapshot/fetch) keep working.
SESSION_END = NFO.session_end

TypesFilterName = str  # "all" | "fut" | "opt"


@dataclass(frozen=True)
class Series:
    underlying: str
    expiry: date
    contracts: tuple[ContractRow, ...]

    @property
    def key(self) -> tuple[str, date]:
        return self.underlying, self.expiry


def _today_ist() -> date:
    return datetime.now(auth.IST).date()


def resolve_market(market: MarketProfile | str | None) -> MarketProfile:
    if market is None:
        return NFO
    if isinstance(market, MarketProfile):
        return market
    return get_market(market)


def resolve_types_filter(types: TypesFilterName | frozenset[str] | None) -> frozenset[str] | None:
    """Map CLI ``all|fut|opt`` (or an explicit frozenset) to an instrument-type set.

    ``None`` / ``\"all\"`` → no extra filter (caller may still apply profile sync types).
    """
    if types is None or types == "all":
        return None
    if isinstance(types, frozenset):
        return types
    key = str(types).strip().lower()
    if key == "fut":
        return FUT_TYPES
    if key == "opt":
        return OPT_TYPES
    if key == "all":
        return None
    raise ValueError(f"unknown types filter={types!r}; expected all|fut|opt")


def filter_series_by_types(
    series: list[Series],
    types: TypesFilterName | frozenset[str] | None,
) -> list[Series]:
    """Keep only contracts matching ``types``; drop series that become empty."""
    allowed = resolve_types_filter(types)
    if allowed is None:
        return series
    out: list[Series] = []
    for s in series:
        contracts = tuple(c for c in s.contracts if c.instrument_type in allowed)
        if contracts:
            out.append(Series(underlying=s.underlying, expiry=s.expiry, contracts=contracts))
    return out


def _expiry_session_end(expiry: date, session_end: time) -> datetime:
    """Timezone-aware IST datetime for end of expiry-day session."""
    return datetime.combine(expiry, session_end, tzinfo=auth.IST)


def sync_contracts(
    con: duckdb.DuckDBPyConnection,
    *,
    market: MarketProfile | str | None = None,
    seen_on: date | None = None,
    allowed_types: frozenset[str] | None = None,
) -> int:
    """Upsert in-scope contracts for ``market`` from the instruments master.

    Default types come from ``MarketProfile.sync_instrument_types`` (NFO: all with
    expiry; MCX/CDS: FUT+CE+PE). ``allowed_types`` overrides that for a single run
    (CLI ``--types fut|opt``); it does not delete existing catalog rows.
    """
    profile = resolve_market(market)
    ensure_cache()
    today = seen_on if seen_on is not None else _today_ist()
    if allowed_types is not None:
        allowed = allowed_types
    else:
        allowed = profile.sync_instrument_types
    rows: list[ContractRow] = []
    for inst in iter_instruments():
        if inst.exchange != profile.exchange or inst.expiry is None:
            continue
        if allowed is not None and inst.instrument_type not in allowed:
            continue
        name = (inst.name or "").strip()
        if not name:
            continue
        rows.append(
            ContractRow(
                token=inst.instrument_token,
                underlying=name,
                expiry=inst.expiry,
                tradingsymbol=inst.tradingsymbol,
                instrument_type=inst.instrument_type,
                strike=float(inst.strike or 0.0),
                lot_size=int(inst.lot_size or 0),
            )
        )
    return upsert_contracts(con, rows, seen_on=today)


def _group_by_series(contracts: Iterable[ContractRow]) -> dict[tuple[str, date], list[ContractRow]]:
    grouped: dict[tuple[str, date], list[ContractRow]] = defaultdict(list)
    for c in contracts:
        grouped[(c.underlying, c.expiry)].append(c)
    return grouped


def _nearest_live_dual_pool(
    grouped: dict[tuple[str, date], list[ContractRow]],
    today: date,
    *,
    contract_filter: frozenset[str] | None = None,
) -> list[Series]:
    """Nearest-3 FUT expiries union nearest-3 CE/PE expiries per underlying.

    Prevents weekly option dates from crowding monthly FUT out of the live set
    (e.g. MCX GOLD). When an expiry appears in both pools, one mixed series is built.
    ``contract_filter`` (CLI ``--types``) limits which contracts appear in each series.
    """
    by_ul_fut: dict[str, set[date]] = defaultdict(set)
    by_ul_opt: dict[str, set[date]] = defaultdict(set)
    for (underlying, expiry), contracts in grouped.items():
        if expiry < today:
            continue
        for c in contracts:
            if c.instrument_type == "FUT":
                by_ul_fut[underlying].add(expiry)
            elif c.instrument_type in OPT_TYPES:
                by_ul_opt[underlying].add(expiry)

    underlyings = set(by_ul_fut) | set(by_ul_opt)
    out: list[Series] = []
    seen_keys: set[tuple[str, date]] = set()
    for underlying in underlyings:
        fut_exps = sorted(by_ul_fut.get(underlying, ()))[:NEAREST_EXPIRIES]
        opt_exps = sorted(by_ul_opt.get(underlying, ()))[:NEAREST_EXPIRIES]
        for expiry in sorted(set(fut_exps) | set(opt_exps)):
            key = (underlying, expiry)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            contracts = grouped.get(key, [])
            if contract_filter is not None:
                contracts = [c for c in contracts if c.instrument_type in contract_filter]
            if not contracts:
                continue
            out.append(
                Series(
                    underlying=underlying,
                    expiry=expiry,
                    contracts=tuple(sorted(contracts, key=lambda c: c.token)),
                )
            )
    return out


def _series_fetch_incomplete(
    contracts: list[ContractRow],
    fetch_states: dict[int, FetchState],
    expiry: date,
    session_end: time,
) -> bool:
    """True if any contract is missing fetch_state, not done/gave_up, or watermark before session end."""
    end = _expiry_session_end(expiry, session_end)
    # Compare naive vs aware carefully: catalog may store naive timestamps.
    session_end_naive = end.replace(tzinfo=None)

    for c in contracts:
        state = fetch_states.get(c.token)
        if state is None:
            return True
        assert isinstance(state, FetchState)
        if state.status == "gave_up":
            continue
        if state.status != "done":
            return True
        if state.fetched_to is None:
            return True
        fetched = state.fetched_to
        if getattr(fetched, "tzinfo", None) is not None:
            if fetched < end:
                return True
        else:
            if fetched < session_end_naive:
                return True
    return False


def select_series(
    con: duckdb.DuckDBPyConnection,
    today: date | None = None,
    *,
    interval: str = "minute",
    market: MarketProfile | str | None = None,
    types: TypesFilterName | frozenset[str] | None = None,
) -> list[Series]:
    """Live dual nearest-3 expiries per underlying, plus unfinished expired series.

    ``types`` filters contracts in both live and unfinished series (CLI ``--types``).
    """
    profile = resolve_market(market)
    today = today if today is not None else _today_ist()
    type_filter = resolve_types_filter(types)
    all_contracts = list_contracts(con)
    if type_filter is not None:
        all_contracts = [c for c in all_contracts if c.instrument_type in type_filter]
    grouped = _group_by_series(all_contracts)

    live = _nearest_live_dual_pool(grouped, today, contract_filter=type_filter)
    live_keys = {s.key for s in live}

    tokens = [c.token for c in all_contracts]
    fetch_states = list_fetch_states(con, interval=interval, tokens=tokens)

    unfinished: list[Series] = []
    for (underlying, expiry), contracts in grouped.items():
        if expiry >= today:
            continue
        if (underlying, expiry) in live_keys:
            continue
        if _series_fetch_incomplete(contracts, fetch_states, expiry, profile.session_end):
            unfinished.append(
                Series(
                    underlying=underlying,
                    expiry=expiry,
                    contracts=tuple(sorted(contracts, key=lambda c: c.token)),
                )
            )

    return live + unfinished


def nearest_fut_tokens(
    con: duckdb.DuckDBPyConnection,
    today: date | None = None,
) -> dict[str, ContractRow]:
    """Map underlying -> nearest live FUT contract (for ranking)."""
    today = today if today is not None else _today_ist()
    best: dict[str, ContractRow] = {}
    for c in list_contracts(con):
        if c.instrument_type != "FUT" or c.expiry < today:
            continue
        prev = best.get(c.underlying)
        if prev is None or c.expiry < prev.expiry:
            best[c.underlying] = c
    return best
