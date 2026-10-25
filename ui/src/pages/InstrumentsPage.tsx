import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  ApiError,
  getInstrumentsMeta,
  listNfoUnderlyings,
  searchInstruments,
} from "../api";
import DataTable, { type Column } from "../components/DataTable";
import ErrorBanner from "../components/ErrorBanner";
import Tabs from "../components/Tabs";
import TokenLink from "../components/TokenLink";
import { formatPrice } from "../format";
import type {
  InstrumentRow,
  InstrumentsMeta,
  NfoUnderlying,
} from "../types";

const TAB_KEY = "instruments_ui_tab_v1";
const PAGE_SIZE = 50;

type TabId = "equity" | "nfo";

function readTab(): TabId {
  try {
    const v = localStorage.getItem(TAB_KEY);
    if (v === "nfo" || v === "equity") return v;
  } catch {
    /* ignore */
  }
  return "equity";
}

export default function InstrumentsPage() {
  const [tab, setTab] = useState<TabId>(readTab);
  const [meta, setMeta] = useState<InstrumentsMeta | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [eqQ, setEqQ] = useState("");
  const [eqExchange, setEqExchange] = useState<"" | "NSE" | "BSE">("");
  const [eqOffset, setEqOffset] = useState(0);
  const [eqTotal, setEqTotal] = useState(0);
  const [eqRows, setEqRows] = useState<InstrumentRow[]>([]);
  const [eqLoading, setEqLoading] = useState(false);

  const [nfoQ, setNfoQ] = useState("");
  const [nfoRows, setNfoRows] = useState<NfoUnderlying[]>([]);
  const [nfoLoading, setNfoLoading] = useState(false);

  useEffect(() => {
    getInstrumentsMeta()
      .then(setMeta)
      .catch((err: unknown) => {
        setError(err instanceof ApiError ? err.message : String(err));
      });
  }, []);

  const loadEquity = useCallback(async () => {
    setEqLoading(true);
    setError(null);
    try {
      const res = await searchInstruments({
        q: eqQ.trim() || undefined,
        exchange: eqExchange || undefined,
        instrument_type: "EQ",
        limit: PAGE_SIZE,
        offset: eqOffset,
      });
      setEqRows(res.items);
      setEqTotal(res.total);
    } catch (err: unknown) {
      setError(err instanceof ApiError ? err.message : String(err));
      setEqRows([]);
      setEqTotal(0);
    } finally {
      setEqLoading(false);
    }
  }, [eqQ, eqExchange, eqOffset]);

  const loadNfo = useCallback(async () => {
    setNfoLoading(true);
    setError(null);
    try {
      const res = await listNfoUnderlyings({
        q: nfoQ.trim() || undefined,
        limit: 200,
      });
      setNfoRows(res.items);
    } catch (err: unknown) {
      setError(err instanceof ApiError ? err.message : String(err));
      setNfoRows([]);
    } finally {
      setNfoLoading(false);
    }
  }, [nfoQ]);

  useEffect(() => {
    if (tab === "equity") void loadEquity();
  }, [tab, loadEquity]);

  useEffect(() => {
    if (tab === "nfo") void loadNfo();
  }, [tab, loadNfo]);

  function changeTab(id: string) {
    const next = id === "nfo" ? "nfo" : "equity";
    setTab(next);
    try {
      localStorage.setItem(TAB_KEY, next);
    } catch {
      /* ignore */
    }
  }

  const equityColumns: Column<InstrumentRow>[] = [
    {
      key: "exchange",
      header: "Exch",
      locked: true,
      sortValue: (r) => r.exchange,
      cell: (r) => r.exchange,
    },
    {
      key: "tradingsymbol",
      header: "Symbol",
      locked: true,
      sortValue: (r) => r.tradingsymbol,
      cell: (r) => (
        <Link to={`/instruments/token/${r.instrument_token}`}>
          {r.tradingsymbol}
        </Link>
      ),
    },
    {
      key: "name",
      header: "Name",
      sortValue: (r) => r.name,
      cell: (r) => r.name || "—",
    },
    {
      key: "lot_size",
      header: "Lot",
      align: "right",
      sortValue: (r) => r.lot_size,
      cell: (r) => r.lot_size,
    },
    {
      key: "tick_size",
      header: "Tick",
      align: "right",
      sortValue: (r) => r.tick_size,
      cell: (r) => r.tick_size,
    },
    {
      key: "last_price",
      header: "Dump LTP",
      align: "right",
      sortValue: (r) => r.last_price,
      cell: (r) => formatPrice(r.last_price),
    },
    {
      key: "instrument_token",
      header: "Token",
      locked: true,
      sortValue: (r) => r.instrument_token,
      cell: (r) => <TokenLink token={r.instrument_token} />,
    },
  ];

  const nfoColumns: Column<NfoUnderlying>[] = [
    {
      key: "name",
      header: "Underlying",
      locked: true,
      sortValue: (r) => r.name,
      cell: (r) => (
        <Link to={`/instruments/nfo/${encodeURIComponent(r.name)}`}>
          {r.name}
        </Link>
      ),
    },
    {
      key: "fut_count",
      header: "FUT",
      align: "right",
      sortValue: (r) => r.fut_count,
      cell: (r) => r.fut_count,
    },
    {
      key: "ce_count",
      header: "CE",
      align: "right",
      sortValue: (r) => r.ce_count,
      cell: (r) => r.ce_count,
    },
    {
      key: "pe_count",
      header: "PE",
      align: "right",
      sortValue: (r) => r.pe_count,
      cell: (r) => r.pe_count,
    },
    {
      key: "total",
      header: "Total",
      align: "right",
      sortValue: (r) => r.total,
      cell: (r) => r.total,
    },
  ];

  const eqPage = Math.floor(eqOffset / PAGE_SIZE) + 1;
  const eqPages = Math.max(1, Math.ceil(eqTotal / PAGE_SIZE));

  return (
    <div>
      <div className="page-header">
        <div>
          <h1>Instruments</h1>
          <p className="page-sub">
            Daily dump browser (NSE/BSE equity, NFO). Prices are not live.
            {meta?.as_of_date ? ` As of ${meta.as_of_date}.` : null}
            {meta ? ` ${meta.total.toLocaleString("en-IN")} instruments.` : null}
          </p>
        </div>
      </div>

      {error ? <ErrorBanner message={error} /> : null}

      <Tabs
        tabs={[
          { id: "equity", label: "Equity", count: meta?.by_instrument_type?.EQ },
          {
            id: "nfo",
            label: "NFO",
            count:
              (meta?.by_exchange?.NFO as number | undefined) ??
              undefined,
          },
        ]}
        active={tab}
        onChange={changeTab}
      />

      {tab === "equity" ? (
        <section className="instruments-panel">
          <div className="instruments-toolbar">
            <input
              className="input"
              type="search"
              placeholder="Search symbol or name…"
              value={eqQ}
              onChange={(e) => {
                setEqOffset(0);
                setEqQ(e.target.value);
              }}
            />
            <select
              className="input"
              value={eqExchange}
              onChange={(e) => {
                setEqOffset(0);
                setEqExchange(e.target.value as "" | "NSE" | "BSE");
              }}
              aria-label="Exchange filter"
            >
              <option value="">All exchanges</option>
              <option value="NSE">NSE</option>
              <option value="BSE">BSE</option>
            </select>
            <span className="toolbar-meta">
              {eqLoading ? "Loading…" : `${eqTotal.toLocaleString("en-IN")} matches`}
            </span>
          </div>
          <DataTable
            columns={equityColumns}
            rows={eqRows}
            rowKey={(r) => String(r.instrument_token)}
            empty={eqLoading ? "Loading…" : "No equity instruments match."}
            storageKey="instruments_ui_cols_equity_v1"
            defaultSortKey="tradingsymbol"
            defaultSortDir="asc"
          />
          <div className="pager">
            <button
              type="button"
              className="btn btn-sm"
              disabled={eqOffset <= 0 || eqLoading}
              onClick={() => setEqOffset(Math.max(0, eqOffset - PAGE_SIZE))}
            >
              Previous
            </button>
            <span>
              Page {eqPage} / {eqPages}
            </span>
            <button
              type="button"
              className="btn btn-sm"
              disabled={eqOffset + PAGE_SIZE >= eqTotal || eqLoading}
              onClick={() => setEqOffset(eqOffset + PAGE_SIZE)}
            >
              Next
            </button>
          </div>
        </section>
      ) : (
        <section className="instruments-panel">
          <div className="instruments-toolbar">
            <input
              className="input"
              type="search"
              placeholder="Search underlying (e.g. NIFTY, TCS)…"
              value={nfoQ}
              onChange={(e) => setNfoQ(e.target.value)}
            />
            <span className="toolbar-meta">
              {nfoLoading
                ? "Loading…"
                : `${nfoRows.length.toLocaleString("en-IN")} underlyings`}
            </span>
          </div>
          <DataTable
            columns={nfoColumns}
            rows={nfoRows}
            rowKey={(r) => r.name}
            empty={nfoLoading ? "Loading…" : "No NFO underlyings match."}
            storageKey="instruments_ui_cols_nfo_v1"
            defaultSortKey="total"
            defaultSortDir="desc"
          />
        </section>
      )}
    </div>
  );
}
