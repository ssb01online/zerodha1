import { useCallback, useEffect, useMemo, useState } from "react";
import { getPreopenDay, listPreopenDays, ApiError } from "../api";
import DataTable, { type Column } from "../components/DataTable";
import ErrorBanner from "../components/ErrorBanner";
import TokenLink from "../components/TokenLink";
import {
  formatInr,
  formatInrCompact,
  formatPrice,
  formatQty,
} from "../format";
import type { PreopenDayPayload, PreopenDayMeta, PreopenRow } from "../types";

const LIMIT_OPTIONS = [25, 50, 100, 200] as const;

export default function PreopenPage() {
  const [days, setDays] = useState<PreopenDayMeta[]>([]);
  const [ymd, setYmd] = useState<string>("");
  const [exchange, setExchange] = useState<"" | "NSE" | "BSE">("");
  const [limit, setLimit] = useState<(typeof LIMIT_OPTIONS)[number]>(50);
  const [payload, setPayload] = useState<PreopenDayPayload | null>(null);
  const [loadingDays, setLoadingDays] = useState(true);
  const [loadingDay, setLoadingDay] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadDays = useCallback(async () => {
    setLoadingDays(true);
    setError(null);
    try {
      const res = await listPreopenDays();
      setDays(res.days);
      setYmd((prev) => {
        if (prev && res.days.some((d) => d.ymd === prev)) return prev;
        return res.days[0]?.ymd ?? "";
      });
    } catch (err: unknown) {
      setDays([]);
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setLoadingDays(false);
    }
  }, []);

  useEffect(() => {
    void loadDays();
  }, [loadDays]);

  const loadDay = useCallback(async () => {
    if (!ymd) {
      setPayload(null);
      return;
    }
    setLoadingDay(true);
    setError(null);
    try {
      const res = await getPreopenDay(ymd, {
        limit,
        exchange: exchange || undefined,
      });
      setPayload(res);
    } catch (err: unknown) {
      setPayload(null);
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setLoadingDay(false);
    }
  }, [ymd, limit, exchange]);

  useEffect(() => {
    void loadDay();
  }, [loadDay]);

  const columns: Column<PreopenRow>[] = useMemo(
    () => [
      {
        key: "symbol",
        header: "Symbol",
        cell: (r) => (
          <TokenLink token={r.token} label={r.tradingsymbol} />
        ),
      },
      {
        key: "exchange",
        header: "Exch",
        cell: (r) => r.exchange,
      },
      {
        key: "name",
        header: "Name",
        cell: (r) => r.name || "—",
      },
      {
        key: "volume",
        header: "Volume",
        align: "right",
        cell: (r) => formatQty(r.volume),
      },
      {
        key: "avg",
        header: "Avg",
        align: "right",
        cell: (r) => formatPrice(r.average_price),
      },
      {
        key: "tv",
        header: "Traded value",
        align: "right",
        cell: (r) => formatInr(r.traded_value),
      },
      {
        key: "open",
        header: "Open",
        align: "right",
        cell: (r) =>
          r.ohlc_open != null ? formatPrice(r.ohlc_open) : "—",
      },
      {
        key: "last",
        header: "Last",
        align: "right",
        cell: (r) =>
          r.last_price != null ? formatPrice(r.last_price) : "—",
      },
    ],
    [],
  );

  return (
    <div>
      <div className="page-header">
        <div>
          <h1>Pre-open</h1>
          <p className="page-sub">
            NSE/BSE equity auction snapshot (~09:08 IST). Ranked by traded
            value (volume × average price).
          </p>
        </div>
        <button
          type="button"
          className="btn"
          disabled={loadingDays || loadingDay}
          onClick={() => {
            void loadDays().then(() => void loadDay());
          }}
        >
          Refresh
        </button>
      </div>

      {error ? (
        <ErrorBanner message={error} onRetry={() => void loadDay()} />
      ) : null}

      <div className="instruments-toolbar">
        <label>
          Day{" "}
          <select
            className="input"
            value={ymd}
            disabled={loadingDays || days.length === 0}
            onChange={(e) => setYmd(e.target.value)}
          >
            {days.length === 0 ? (
              <option value="">No snapshots</option>
            ) : (
              days.map((d) => (
                <option key={d.ymd} value={d.ymd}>
                  {d.trade_date}
                </option>
              ))
            )}
          </select>
        </label>
        <label>
          Exchange{" "}
          <select
            className="input"
            value={exchange}
            onChange={(e) =>
              setExchange(e.target.value as "" | "NSE" | "BSE")
            }
          >
            <option value="">All</option>
            <option value="NSE">NSE</option>
            <option value="BSE">BSE</option>
          </select>
        </label>
        <label>
          Top{" "}
          <select
            className="input"
            value={limit}
            onChange={(e) =>
              setLimit(Number(e.target.value) as (typeof LIMIT_OPTIONS)[number])
            }
          >
            {LIMIT_OPTIONS.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
        <span className="toolbar-meta">
          {payload
            ? `${payload.filtered_rows.toLocaleString("en-IN")} matching · day ${payload.day_traded_rows.toLocaleString("en-IN")} traded`
            : loadingDays
              ? "Loading days…"
              : "—"}
        </span>
      </div>

      {payload ? (
        <div className="summary-grid" style={{ marginBottom: "1rem" }}>
          <div className="summary-card">
            <div className="label">Day traded value</div>
            <div className="value">
              {formatInrCompact(payload.day_total_traded_value)}
            </div>
            <div className="sub">
              {formatInr(payload.day_total_traded_value)}
            </div>
          </div>
          <div className="summary-card">
            <div className="label">Filtered traded value</div>
            <div className="value">
              {formatInrCompact(payload.filtered_total_traded_value)}
            </div>
            <div className="sub">
              Top {payload.limit}
              {exchange ? ` · ${exchange}` : ""}
            </div>
          </div>
          <div className="summary-card">
            <div className="label">Rows</div>
            <div className="value">
              {payload.day_rows.toLocaleString("en-IN")}
            </div>
            <div className="sub">
              Traded {payload.day_traded_rows.toLocaleString("en-IN")}
            </div>
          </div>
        </div>
      ) : null}

      {loadingDay && !payload ? (
        <div className="loading">Loading pre-open snapshot…</div>
      ) : (
        <DataTable
          columns={columns}
          rows={payload?.items ?? []}
          rowKey={(r) => `${r.exchange}:${r.token}`}
          empty={
            <div className="empty-state">
              {days.length === 0
                ? "No pre-open snapshots yet. Run: uv run preopen capture"
                : "No rows match the current filters."}
            </div>
          }
        />
      )}
    </div>
  );
}
