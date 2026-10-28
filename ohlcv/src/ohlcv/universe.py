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
from .markets import MarketProfile, NFO, get_market

NEAREST_EXPIRIES = 3
# NFO alias — existing imports (planner/snapshot/fetch) keep working.
SESSION_END = NFO.session_end


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


def _expiry_session_end(expiry: date, session_end: time) -> datetime:
    """Timezone-aware IST datetime for end of expiry-day session."""
    return datetime.combine(expiry, session_end, tzinfo=auth.IST)


def sync_contracts(
    con: duckdb.DuckDBPyConnection,
    *,
    market: MarketProfile | str | None = None,
    seen_on: date | None = None,
) -> int:
    """Upsert in-scope contracts for ``market`` from the instruments master.

    NFO: all rows with expiry (FUT + options). MCX/CDS: FUT only (Phase 2).
    """
    profile = resolve_market(market)
    ensure_cache()
    today = seen_on if seen_on is not None else _today_ist()
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


def _nearest_three_live(
    grouped: dict[tuple[str, date], list[ContractRow]],
    today: date,
) -> list[Series]:
    by_underlying: dict[str, list[date]] = defaultdict(list)
    for (underlying, expiry), _ in grouped.items():
        if expiry >= today:
            by_underlying[underlying].append(expiry)

    out: list[Series] = []
    for underlying, expiries in by_underlying.items():
        for expiry in sorted(set(expiries))[:NEAREST_EXPIRIES]:
            contracts = tuple(sorted(grouped[(underlying, expiry)], key=lambda c: c.token))
            out.append(Series(underlying=underlying, expiry=expiry, contracts=contracts))
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
) -> list[Series]:
    """Live nearest-3 expiries per underlying, plus unfinished expired series from catalog."""
    profile = resolve_market(market)
    today = today if today is not None else _today_ist()
    all_contracts = list_contracts(con)
    grouped = _group_by_series(all_contracts)

    live = _nearest_three_live(grouped, today)
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
