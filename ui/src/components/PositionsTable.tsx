import { useEffect, useState } from "react";
import { formatExpiryChip, formatInr, formatPrice, formatQty, pnlClass } from "../format";
import {
  collectExpiryKeys,
  filterByExpiry,
  filterByKind,
  type ExpiryKey,
  type PositionKind,
} from "../positionsFilters";
import type { PositionRow } from "../types";
import DataTable, { type Column } from "./DataTable";

interface PositionsTableProps {
  net: PositionRow[];
  day: PositionRow[];
}

const FILTERS_KEY = "portfolio_ui_pos_filters_v1";

interface StoredFilters {
  kind?: PositionKind;
  expiryKey?: ExpiryKey;
}

function loadFilters(): { kind: PositionKind; expiryKey: ExpiryKey } {
  try {
    const raw = localStorage.getItem(FILTERS_KEY);
    if (!raw) return { kind: "all", expiryKey: "all" };
    const parsed = JSON.parse(raw) as StoredFilters;
    const kind: PositionKind =
      parsed.kind === "equity" || parsed.kind === "fo" || parsed.kind === "all"
        ? parsed.kind
        : "all";
    const expiryKey: ExpiryKey =
      typeof parsed.expiryKey === "string" && parsed.expiryKey.length > 0
        ? parsed.expiryKey
        : "all";
    return { kind, expiryKey };
  } catch {
    return { kind: "all", expiryKey: "all" };
  }
}

export default function PositionsTable({ net, day }: PositionsTableProps) {
  const [mode, setMode] = useState<"net" | "day">("net");
  const [showClosed, setShowClosed] = useState(false);
  const [kind, setKind] = useState<PositionKind>(() => loadFilters().kind);
  const [expiryKey, setExpiryKey] = useState<ExpiryKey>(
    () => loadFilters().expiryKey,
  );

  const source = mode === "net" ? net : day;
  const afterKind = filterByKind(source, kind);
  const expiryKeys = collectExpiryKeys(source);
  const showExpiry = kind !== "equity" && expiryKeys.length > 0;
  const effectiveExpiry: ExpiryKey =
    kind === "equity"
      ? "all"
      : expiryKey === "all" ||
          (showExpiry && expiryKeys.includes(expiryKey))
        ? expiryKey
        : "all";
  const afterExpiry =
    kind === "equity"
      ? afterKind
      : filterByExpiry(afterKind, effectiveExpiry);

  useEffect(() => {
    localStorage.setItem(
      FILTERS_KEY,
      JSON.stringify({
        kind,
        expiryKey: effectiveExpiry,
      } satisfies StoredFilters),
    );
  }, [kind, effectiveExpiry]);

  const closedCount = afterExpiry.filter((r) => !r.is_open).length;
  const rows = showClosed ? afterExpiry : afterExpiry.filter((r) => r.is_open);
  const filteredOut =
    source.length > 0 && afterExpiry.length === 0;

  const setKindAndClear = (next: PositionKind) => {
    setKind(next);
    if (next === "equity") setExpiryKey("all");
  };

  const columns: Column<PositionRow>[] = [
    {
      key: "symbol",
      header: "Symbol",
      locked: true,
      sortValue: (r) => r.tradingsymbol,
      cell: (r) => (
        <>
          <span className="cell-primary">{r.tradingsymbol}</span>
          <span className="cell-secondary">{r.exchange}</span>
        </>
      ),
    },
    {
      key: "product",
      header: "Product",
      sortValue: (r) => r.product,
      cell: (r) => <span className="badge badge-product">{r.product}</span>,
    },
    {
      key: "qty",
      header: "Qty",
      align: "right",
      sortValue: (r) => r.quantity,
      cell: (r) => (
        <span className={r.quantity < 0 ? "pnl-neg" : undefined}>
          {formatQty(r.quantity)}
        </span>
      ),
    },
    {
      key: "overnight",
      header: "Overnight",
      align: "right",
      sortValue: (r) => r.overnight_quantity,
      cell: (r) => formatQty(r.overnight_quantity),
    },
    {
      key: "avg",
      header: "Avg",
      align: "right",
      sortValue: (r) => r.average_price,
      cell: (r) => formatPrice(r.average_price),
    },
    {
      key: "ltp",
      header: "LTP",
      align: "right",
      sortValue: (r) => r.last_price,
      cell: (r) => formatPrice(r.last_price),
    },
    {
      key: "pnl",
      header: "P&L",
      align: "right",
      sortValue: (r) => r.pnl,
      cell: (r) => (
        <span className={pnlClass(r.pnl)}>{formatInr(r.pnl)}</span>
      ),
    },
    {
      key: "m2m",
      header: "M2M",
      align: "right",
      sortValue: (r) => r.m2m,
      cell: (r) => (
        <span className={pnlClass(r.m2m)}>{formatInr(r.m2m)}</span>
      ),
    },
    {
      key: "realised",
      header: "Realised",
      align: "right",
      sortValue: (r) => r.realised,
      cell: (r) => (
        <span className={pnlClass(r.realised)}>{formatInr(r.realised)}</span>
      ),
    },
    {
      key: "unrealised",
      header: "Unrealised",
      align: "right",
      sortValue: (r) => r.unrealised,
      cell: (r) => (
        <span className={pnlClass(r.unrealised)}>
          {formatInr(r.unrealised)}
        </span>
      ),
    },
  ];

  let emptyMessage = "No open positions";
  if (filteredOut) {
    emptyMessage = "No positions match filters";
  } else if (!showClosed && closedCount > 0) {
    emptyMessage =
      "No open positions — use Show closed to see flat rows";
  }

  return (
    <div>
      <div className="sub-toggle">
        <div className="segmented">
          <button
            type="button"
            className={mode === "net" ? "active" : undefined}
            onClick={() => setMode("net")}
          >
            Net
          </button>
          <button
            type="button"
            className={mode === "day" ? "active" : undefined}
            onClick={() => setMode("day")}
          >
            Day
          </button>
        </div>
        <div className="segmented">
          <button
            type="button"
            className={kind === "all" ? "active" : undefined}
            onClick={() => setKindAndClear("all")}
          >
            All
          </button>
          <button
            type="button"
            className={kind === "equity" ? "active" : undefined}
            onClick={() => setKindAndClear("equity")}
          >
            Equity
          </button>
          <button
            type="button"
            className={kind === "fo" ? "active" : undefined}
            onClick={() => setKindAndClear("fo")}
          >
            F&amp;O
          </button>
        </div>
        {showExpiry ? (
          <div className="segmented">
            <button
              type="button"
              className={effectiveExpiry === "all" ? "active" : undefined}
              onClick={() => setExpiryKey("all")}
            >
              All
            </button>
            {expiryKeys.map((key) => (
              <button
                key={key}
                type="button"
                className={effectiveExpiry === key ? "active" : undefined}
                onClick={() => setExpiryKey(key)}
              >
                {key === "unknown" ? "Unknown" : formatExpiryChip(key)}
              </button>
            ))}
          </div>
        ) : null}
        {closedCount > 0 ? (
          <button
            type="button"
            className="btn btn-ghost btn-sm sub-toggle-end"
            onClick={() => setShowClosed((v) => !v)}
          >
            {showClosed
              ? "Hide closed"
              : `Show closed (${closedCount})`}
          </button>
        ) : null}
      </div>
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) =>
          `${r.exchange}:${r.tradingsymbol}:${r.product}:${r.instrument_token}`
        }
        empty={<div className="empty-state">{emptyMessage}</div>}
        storageKey="portfolio_ui_cols_positions_v1"
        defaultSortKey="pnl"
        defaultSortDir="desc"
      />
    </div>
  );
}
