import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { ApiError, getNfoChain, listNfoExpiries } from "../api";
import DataTable, { type Column } from "../components/DataTable";
import ErrorBanner from "../components/ErrorBanner";
import TokenLink from "../components/TokenLink";
import { formatPrice } from "../format";
import type { OptionChainResult, OptionChainStrike } from "../types";

export default function NfoChainPage() {
  const { underlying: raw } = useParams();
  const underlying = decodeURIComponent(raw ?? "");
  const [searchParams, setSearchParams] = useSearchParams();
  const expiryParam = searchParams.get("expiry") ?? "";

  const [expiries, setExpiries] = useState<string[]>([]);
  const [chain, setChain] = useState<OptionChainResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!underlying) return;
    setError(null);
    listNfoExpiries(underlying)
      .then((res) => {
        setExpiries(res.expiries);
        if (res.expiries.length === 0) return;
        const current = searchParams.get("expiry") ?? "";
        if (!current || !res.expiries.includes(current)) {
          setSearchParams({ expiry: res.expiries[0] }, { replace: true });
        }
      })
      .catch((err: unknown) => {
        setError(err instanceof ApiError ? err.message : String(err));
      });
    // Only re-fetch expiries when underlying changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps -- intentional
  }, [underlying, setSearchParams]);

  const loadChain = useCallback(async () => {
    if (!underlying || !expiryParam) {
      setChain(null);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      setChain(await getNfoChain(underlying, expiryParam));
    } catch (err: unknown) {
      setError(err instanceof ApiError ? err.message : String(err));
      setChain(null);
    } finally {
      setLoading(false);
    }
  }, [underlying, expiryParam]);

  useEffect(() => {
    void loadChain();
  }, [loadChain]);

  const columns: Column<OptionChainStrike>[] = useMemo(
    () => [
      {
        key: "ce_token",
        header: "CE token",
        cell: (r) =>
          r.ce ? <TokenLink token={r.ce.instrument_token} /> : "—",
        sortValue: (r) => r.ce?.instrument_token ?? null,
      },
      {
        key: "ce_symbol",
        header: "CE",
        cell: (r) =>
          r.ce ? (
            <Link to={`/instruments/token/${r.ce.instrument_token}`}>
              {r.ce.tradingsymbol}
            </Link>
          ) : (
            "—"
          ),
        sortValue: (r) => r.ce?.tradingsymbol ?? null,
      },
      {
        key: "ce_ltp",
        header: "CE dump",
        align: "right",
        cell: (r) => (r.ce ? formatPrice(r.ce.last_price) : "—"),
        sortValue: (r) => r.ce?.last_price ?? null,
      },
      {
        key: "strike",
        header: "Strike",
        align: "right",
        locked: true,
        cell: (r) => (
          <span className="strike-cell">{r.strike.toLocaleString("en-IN")}</span>
        ),
        sortValue: (r) => r.strike,
      },
      {
        key: "pe_ltp",
        header: "PE dump",
        align: "right",
        cell: (r) => (r.pe ? formatPrice(r.pe.last_price) : "—"),
        sortValue: (r) => r.pe?.last_price ?? null,
      },
      {
        key: "pe_symbol",
        header: "PE",
        cell: (r) =>
          r.pe ? (
            <Link to={`/instruments/token/${r.pe.instrument_token}`}>
              {r.pe.tradingsymbol}
            </Link>
          ) : (
            "—"
          ),
        sortValue: (r) => r.pe?.tradingsymbol ?? null,
      },
      {
        key: "pe_token",
        header: "PE token",
        cell: (r) =>
          r.pe ? <TokenLink token={r.pe.instrument_token} /> : "—",
        sortValue: (r) => r.pe?.instrument_token ?? null,
      },
    ],
    [],
  );

  return (
    <div>
      <div className="page-header">
        <div>
          <p className="page-sub">
            <Link to="/instruments">Instruments</Link>
            {" / NFO / "}
            {underlying}
          </p>
          <h1>{underlying}</h1>
          <p className="page-sub">
            Option chain from daily dump
            {chain?.as_of_date ? ` (as of ${chain.as_of_date})` : ""}.
          </p>
        </div>
      </div>

      {error ? <ErrorBanner message={error} /> : null}

      <div className="expiry-pills" role="tablist" aria-label="Expiry">
        {expiries.map((exp) => (
          <button
            key={exp}
            type="button"
            role="tab"
            aria-selected={exp === expiryParam}
            className={`expiry-pill${exp === expiryParam ? " active" : ""}`}
            onClick={() => setSearchParams({ expiry: exp })}
          >
            {exp}
          </button>
        ))}
        {expiries.length === 0 && !error ? (
          <span className="toolbar-meta">No expiries found.</span>
        ) : null}
      </div>

      {chain?.fut ? (
        <div className="fut-strip status-bar">
          <span className="tag">FUT</span>
          <Link to={`/instruments/token/${chain.fut.instrument_token}`}>
            {chain.fut.tradingsymbol}
          </Link>
          <TokenLink token={chain.fut.instrument_token} />
          <span>Lot {chain.fut.lot_size}</span>
          <span>Dump LTP {formatPrice(chain.fut.last_price)}</span>
        </div>
      ) : null}

      <DataTable
        columns={columns}
        rows={chain?.strikes ?? []}
        rowKey={(r) => String(r.strike)}
        empty={loading ? "Loading…" : "No option strikes for this expiry."}
        storageKey="instruments_ui_cols_chain_v1"
        defaultSortKey="strike"
        defaultSortDir="asc"
      />
    </div>
  );
}
