import {
  formatInr,
  formatPct,
  formatPrice,
  formatUnits,
  pnlClass,
} from "../format";
import type { MfRow } from "../types";
import DataTable, { type Column } from "./DataTable";

interface MfTableProps {
  rows: MfRow[];
}

export default function MfTable({ rows }: MfTableProps) {
  const totals = rows.reduce(
    (acc, r) => {
      acc.invested += r.invested;
      acc.current_value += r.current_value;
      acc.pnl += r.pnl;
      return acc;
    },
    { invested: 0, current_value: 0, pnl: 0 },
  );
  const pnlPct =
    totals.invested !== 0 ? (totals.pnl / totals.invested) * 100 : null;

  const columns: Column<MfRow>[] = [
    {
      key: "fund",
      header: "Fund",
      locked: true,
      sortValue: (r) => r.fund || r.tradingsymbol,
      cell: (r) => (
        <>
          <span className="cell-primary">{r.fund || r.tradingsymbol}</span>
          <span className="cell-secondary">
            Folio {r.folio || "—"} · {r.isin}
          </span>
        </>
      ),
      footer: "Total",
    },
    {
      key: "units",
      header: "Units",
      align: "right",
      sortValue: (r) => r.quantity,
      cell: (r) => formatUnits(r.quantity),
    },
    {
      key: "avg",
      header: "Avg NAV",
      align: "right",
      sortValue: (r) => r.average_price,
      cell: (r) => formatPrice(r.average_price),
    },
    {
      key: "nav",
      header: "NAV",
      align: "right",
      sortValue: (r) => r.last_price,
      cell: (r) => formatPrice(r.last_price),
    },
    {
      key: "invested",
      header: "Invested",
      align: "right",
      sortValue: (r) => r.invested,
      cell: (r) => formatInr(r.invested),
      footer: formatInr(totals.invested),
    },
    {
      key: "value",
      header: "Value",
      align: "right",
      sortValue: (r) => r.current_value,
      cell: (r) => formatInr(r.current_value),
      footer: formatInr(totals.current_value),
    },
    {
      key: "pnl",
      header: "P&L",
      align: "right",
      sortValue: (r) => r.pnl,
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
  ];

  return (
    <DataTable
      columns={columns}
      rows={rows}
      rowKey={(r) => `${r.isin}:${r.folio}`}
      empty={<div className="empty-state">No mutual fund holdings</div>}
      storageKey="portfolio_ui_cols_mf_v1"
      defaultSortKey="value"
      defaultSortDir="desc"
    />
  );
}
