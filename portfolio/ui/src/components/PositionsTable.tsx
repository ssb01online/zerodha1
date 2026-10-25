import { useState } from "react";
import { formatInr, formatPrice, formatQty, pnlClass } from "../format";
import type { PositionRow } from "../types";
import DataTable, { type Column } from "./DataTable";

interface PositionsTableProps {
  net: PositionRow[];
  day: PositionRow[];
}

export default function PositionsTable({ net, day }: PositionsTableProps) {
  const [mode, setMode] = useState<"net" | "day">("net");
  const [showClosed, setShowClosed] = useState(false);

  const source = mode === "net" ? net : day;
  const closedCount = source.filter((r) => !r.is_open).length;
  const rows = showClosed ? source : source.filter((r) => r.is_open);

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
        {closedCount > 0 ? (
          <button
            type="button"
            className="btn btn-ghost btn-sm"
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
        empty={
          <div className="empty-state">
            {showClosed || closedCount === 0
              ? "No open positions"
              : "No open positions — use Show closed to see flat rows"}
          </div>
        }
        storageKey="portfolio_ui_cols_positions_v1"
        defaultSortKey="pnl"
        defaultSortDir="desc"
      />
    </div>
  );
}
