import {
  formatInr,
  formatPct,
  formatPrice,
  formatQty,
  pnlClass,
} from "../format";
import type { HoldingRow } from "../types";
import DataTable, { type Column } from "./DataTable";

interface HoldingsTableProps {
  rows: HoldingRow[];
}

export default function HoldingsTable({ rows }: HoldingsTableProps) {
  const sorted = [...rows].sort((a, b) => b.current_value - a.current_value);

  const totals = sorted.reduce(
    (acc, r) => {
      acc.invested += r.invested;
      acc.current_value += r.current_value;
      acc.pnl += r.pnl;
      acc.day_change_value += r.day_change_value;
      return acc;
    },
    { invested: 0, current_value: 0, pnl: 0, day_change_value: 0 },
  );
  const pnlPct =
    totals.invested !== 0 ? (totals.pnl / totals.invested) * 100 : null;

  const columns: Column<HoldingRow>[] = [
    {
      key: "symbol",
      header: "Symbol",
      cell: (r) => (
        <>
          <span className="cell-primary">
            {r.tradingsymbol}
            {r.t1_quantity > 0 ? <span className="badge">T1</span> : null}
            {r.collateral_quantity > 0 ? (
              <span className="badge">Pledged {formatQty(r.collateral_quantity)}</span>
            ) : null}
          </span>
          <span className="cell-secondary">
            {r.exchange} · {r.isin}
          </span>
        </>
      ),
      footer: "Total",
    },
    {
      key: "qty",
      header: "Qty",
      align: "right",
      cell: (r) => formatQty(r.owned_quantity),
    },
    {
      key: "avg",
      header: "Avg cost",
      align: "right",
      cell: (r) => formatPrice(r.average_price),
    },
    {
      key: "ltp",
      header: "LTP",
      align: "right",
      cell: (r) => formatPrice(r.last_price),
    },
    {
      key: "invested",
      header: "Invested",
      align: "right",
      cell: (r) => formatInr(r.invested),
      footer: formatInr(totals.invested),
    },
    {
      key: "value",
      header: "Value",
      align: "right",
      cell: (r) => formatInr(r.current_value),
      footer: formatInr(totals.current_value),
    },
    {
      key: "pnl",
      header: "P&L",
      align: "right",
      cell: (r) => (
        <span className={pnlClass(r.pnl)}>
          {formatInr(r.pnl)}
          <span className="cell-secondary">{formatPct(r.pnl_pct)}</span>
        </span>
      ),
      footer: (
        <span className={pnlClass(totals.pnl)}>
          {formatInr(totals.pnl)}
          <span className="cell-secondary">{formatPct(pnlPct)}</span>
        </span>
      ),
    },
    {
      key: "day",
      header: "Day chg",
      align: "right",
      cell: (r) => (
        <span className={pnlClass(r.day_change_value)}>
          {formatInr(r.day_change_value)}
          <span className="cell-secondary">
            {formatPct(r.day_change_percentage)}
          </span>
        </span>
      ),
      footer: (
        <span className={pnlClass(totals.day_change_value)}>
          {formatInr(totals.day_change_value)}
        </span>
      ),
    },
    {
      key: "weight",
      header: "Weight",
      align: "right",
      cell: (r) =>
        r.weight === null || r.weight === undefined
          ? "—"
          : formatPct(r.weight * 100),
    },
  ];

  return (
    <DataTable
      columns={columns}
      rows={sorted}
      rowKey={(r) => r.isin || `${r.exchange}:${r.tradingsymbol}`}
      empty={<div className="empty-state">No equity holdings</div>}
    />
  );
}
