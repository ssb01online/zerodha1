"""Composition fingerprint — the single place that encodes the frozen Phase 1 rule.

See ref/kite/notes.md "Snapshot fingerprint (frozen for Phase 1)".
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .models import PortfolioView


def composition(view: PortfolioView) -> dict[str, float]:
    """Build the composition map used for change detection.

    Keys:
      EQ:{isin}                              equity holdings owned qty
      EQ:{exchange}:{tradingsymbol}:CNC      CNC net position when ISIN unknown
      POS:{exchange}:{tradingsymbol}:{product}  F&O / MIS net quantity (signed)
      MF:{isin}:{folio}                      MF units rounded to 3 decimals
    """
    comp: dict[str, float] = {}

    # Holdings: EQ:{isin}
    symbol_to_isin: dict[str, str] = {}
    for h in view.holdings:
        if h.isin:
            key = f"EQ:{h.isin}"
            comp[key] = comp.get(key, 0.0) + float(h.owned_quantity)
            symbol_to_isin[f"{h.exchange}:{h.tradingsymbol}"] = h.isin
        else:
            # No ISIN — fall back to exchange:symbol so we never drop the row.
            key = f"EQ:{h.exchange}:{h.tradingsymbol}:CNC"
            comp[key] = comp.get(key, 0.0) + float(h.owned_quantity)

    # Positions.net
    for p in view.positions_net:
        product = (p.product or "").upper()
        qty = float(p.quantity)
        if product == "CNC":
            lookup = f"{p.exchange}:{p.tradingsymbol}"
            isin = symbol_to_isin.get(lookup)
            if isin:
                key = f"EQ:{isin}"
            else:
                key = f"EQ:{p.exchange}:{p.tradingsymbol}:CNC"
            comp[key] = comp.get(key, 0.0) + qty
        else:
            key = f"POS:{p.exchange}:{p.tradingsymbol}:{p.product}"
            comp[key] = comp.get(key, 0.0) + qty

    # MF holdings
    for m in view.mf_holdings:
        isin = m.isin or m.tradingsymbol
        key = f"MF:{isin}:{m.folio}"
        comp[key] = comp.get(key, 0.0) + round(float(m.quantity), 3)

    # Drop zeros after aggregation
    return {k: v for k, v in comp.items() if v != 0}


def fingerprint_hash(comp: dict[str, float]) -> str:
    """SHA-256 of canonical sorted JSON (stable across processes)."""
    canonical = json.dumps(comp, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CompositionDiff:
    added: dict[str, float]
    removed: dict[str, float]
    changed: dict[str, tuple[float, float]]  # key -> (old, new)

    @property
    def empty(self) -> bool:
        return not self.added and not self.removed and not self.changed

    def short_summary(self, limit: int = 8) -> str:
        parts: list[str] = []
        for key, qty in self.added.items():
            parts.append(f"+{key}={qty}")
        for key, qty in self.removed.items():
            parts.append(f"-{key}={qty}")
        for key, (old, new) in self.changed.items():
            parts.append(f"~{key}:{old}->{new}")
        if len(parts) > limit:
            more = len(parts) - limit
            parts = parts[:limit]
            parts.append(f"... +{more} more")
        return ", ".join(parts) if parts else "(no changes)"


def diff(old: dict[str, float], new: dict[str, float]) -> CompositionDiff:
    added: dict[str, float] = {}
    removed: dict[str, float] = {}
    changed: dict[str, tuple[float, float]] = {}
    all_keys = set(old) | set(new)
    for key in sorted(all_keys):
        o = old.get(key)
        n = new.get(key)
        if o is None and n is not None:
            added[key] = n
        elif o is not None and n is None:
            removed[key] = o
        elif o is not None and n is not None and o != n:
            changed[key] = (o, n)
    return CompositionDiff(added=added, removed=removed, changed=changed)


def book_for_key(key: str) -> str:
    """Map a composition key prefix to a book label for storage."""
    if key.startswith("EQ:"):
        return "EQ"
    if key.startswith("POS:"):
        return "POS"
    if key.startswith("MF:"):
        return "MF"
    return "OTHER"


def as_jsonable(comp: dict[str, float]) -> dict[str, Any]:
    return dict(sorted(comp.items()))
