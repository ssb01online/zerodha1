import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, getInstrumentByToken } from "../api";
import ErrorBanner from "../components/ErrorBanner";
import { formatPrice } from "../format";
import type { InstrumentRow } from "../types";

const FIELDS: { key: keyof InstrumentRow; label: string }[] = [
  { key: "instrument_token", label: "Instrument token" },
  { key: "exchange", label: "Exchange" },
  { key: "tradingsymbol", label: "Trading symbol" },
  { key: "name", label: "Name" },
  { key: "instrument_type", label: "Type" },
  { key: "segment", label: "Segment" },
  { key: "expiry", label: "Expiry" },
  { key: "strike", label: "Strike" },
  { key: "lot_size", label: "Lot size" },
  { key: "tick_size", label: "Tick size" },
  { key: "last_price", label: "Dump last price" },
  { key: "exchange_token", label: "Exchange token" },
];

function displayValue(row: InstrumentRow, key: keyof InstrumentRow): string {
  const v = row[key];
  if (v === null || v === undefined || v === "") return "—";
  if (key === "last_price" || key === "strike" || key === "tick_size") {
    return formatPrice(Number(v));
  }
  return String(v);
}

export default function InstrumentDetailPage() {
  const { token: raw } = useParams();
  const token = Number(raw);
  const [row, setRow] = useState<InstrumentRow | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!Number.isFinite(token) || token <= 0) {
      setError("Invalid instrument token.");
      return;
    }
    setError(null);
    getInstrumentByToken(token)
      .then(setRow)
      .catch((err: unknown) => {
        setRow(null);
        setError(err instanceof ApiError ? err.message : String(err));
      });
  }, [token]);

  async function copyToken() {
    if (!row) return;
    try {
      await navigator.clipboard.writeText(String(row.instrument_token));
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      /* ignore */
    }
  }

  const identity =
    row != null ? `${row.exchange}:${row.tradingsymbol}` : null;

  return (
    <div>
      <div className="page-header">
        <div>
          <p className="page-sub">
            <Link to="/instruments">Instruments</Link>
            {" / token "}
            {Number.isFinite(token) ? token : "—"}
          </p>
          <h1>{row?.tradingsymbol ?? "Instrument"}</h1>
          <p className="page-sub">
            {identity ? (
              <>
                Identity <span className="mono">{identity}</span> · dump fields
                only
              </>
            ) : (
              "Loading…"
            )}
          </p>
        </div>
        {row ? (
          <button type="button" className="btn btn-primary" onClick={copyToken}>
            {copied ? "Copied" : "Copy token"}
          </button>
        ) : null}
      </div>

      {error ? <ErrorBanner message={error} /> : null}

      {row ? (
        <dl className="detail-grid">
          {FIELDS.map(({ key, label }) => (
            <div key={key} className="detail-row">
              <dt>{label}</dt>
              <dd className={key === "instrument_token" ? "mono" : undefined}>
                {displayValue(row, key)}
              </dd>
            </div>
          ))}
          <div className="detail-row">
            <dt>Exchange:symbol</dt>
            <dd className="mono">{identity}</dd>
          </div>
        </dl>
      ) : null}

      {row?.exchange === "NFO" && row.name ? (
        <p className="page-sub" style={{ marginTop: "1.25rem" }}>
          <Link
            to={`/instruments/nfo/${encodeURIComponent(row.name)}${
              row.expiry ? `?expiry=${row.expiry}` : ""
            }`}
          >
            Open {row.name} option chain
          </Link>
        </p>
      ) : null}
    </div>
  );
}
