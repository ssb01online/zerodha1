import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  ApiError,
  getQualityContract,
  getQualitySummary,
  listQualityReports,
} from "../api";
import DataTable, { type Column } from "../components/DataTable";
import ErrorBanner from "../components/ErrorBanner";
import { formatInrCompact, formatIst, formatQty } from "../format";
import type { QualityContract, QualityReportMeta, QualityRow, QualitySummary } from "../types";

const DAY_PRESETS = [60, 90] as const;

function formatPct(value: number | null): string {
  if (value == null) return "n/a";
  return `${Math.round(value)}%`;
}

function formatCount(value: number): string {
  return value.toLocaleString("en-IN");
}

function parseDays(raw: string | null): number | null {
  if (raw == null || raw.trim() === "") return null;
  const n = Number(raw);
  if (!Number.isInteger(n) || n < 1) return null;
  return n;
}

function rowLabel(row: QualityRow, level: QualitySummary["level"]): string {
  if (level === "market") return row.market;
  if (level === "underlying") return row.underlying ?? "";
  if (level === "expiry" || level === "date") return row.expiry ?? "";
  return row.tradingsymbol ?? String(row.token ?? "");
}

export default function QualityPage() {
  const [params, setParams] = useSearchParams();
  const [reports, setReports] = useState<QualityReportMeta[]>([]);
  const [summary, setSummary] = useState<QualitySummary | null>(null);
  const [contract, setContract] = useState<QualityContract | null>(null);
  const [loadingReports, setLoadingReports] = useState(true);
  const [loadingSummary, setLoadingSummary] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [daysFocused, setDaysFocused] = useState(false);
  const [daysDraft, setDaysDraft] = useState("");

  const reportId = params.get("report") ?? "";
  const daysQuery = params.get("days") ?? "";
  const market = params.get("market") ?? "";
  const underlying = params.get("underlying") ?? "";
  const expiry = params.get("expiry") ?? "";
  const by = params.get("by") === "expiry" ? "expiry" : "security";
  const tokenRaw = params.get("token") ?? "";
  const token = tokenRaw !== "" && Number.isInteger(Number(tokenRaw)) ? Number(tokenRaw) : null;
  const days = parseDays(daysQuery);

  const writeParams = useCallback(
    (patch: Record<string, string | null>, replace = false) => {
      const next = new URLSearchParams(params);
      for (const [key, value] of Object.entries(patch)) {
        if (value == null || value === "") next.delete(key);
        else next.set(key, value);
      }
      setParams(next, { replace });
    },
    [params, setParams],
  );

  const loadReports = useCallback(async () => {
    setLoadingReports(true);
    setError(null);
    try {
      const res = await listQualityReports();
      setReports(res.reports);
    } catch (err: unknown) {
      setReports([]);
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setLoadingReports(false);
    }
  }, []);

  useEffect(() => {
    void loadReports();
  }, [loadReports]);

  useEffect(() => {
    if (!daysFocused) setDaysDraft(daysQuery);
  }, [daysQuery, daysFocused]);

  useEffect(() => {
    if (loadingReports || reports.length === 0) return;
    const known = reports.some((report) => report.id === reportId);
    if (known) return;
    const next = new URLSearchParams(params);
    next.set("report", reports[0].id);
    next.delete("market");
    next.delete("underlying");
    next.delete("expiry");
    next.delete("token");
    setParams(next, { replace: true });
  }, [loadingReports, reports, reportId, params, setParams]);

  const selected = reports.find((report) => report.id === reportId) ?? null;

  useEffect(() => {
    if (!selected) {
      setSummary(null);
      return;
    }
    let cancelled = false;
    setLoadingSummary(true);
    setError(null);
    getQualitySummary(selected.id, {
      days,
      market: market || undefined,
      underlying: underlying || undefined,
      expiry: expiry || undefined,
      by,
    })
      .then((res) => {
        if (!cancelled) setSummary(res);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setSummary(null);
        setError(err instanceof ApiError ? err.message : String(err));
      })
      .finally(() => {
        if (!cancelled) setLoadingSummary(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selected, days, market, underlying, expiry, by]);

  useEffect(() => {
    if (!selected || token == null) {
      setContract(null);
      return;
    }
    let cancelled = false;
    getQualityContract(selected.id, token, {
      days,
      market: market || undefined,
    })
      .then((res) => {
        if (!cancelled) setContract(res);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setContract(null);
        setError(err instanceof ApiError ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [selected, token, days, market]);

  const level = summary?.level ?? "market";

  const columns: Column<QualityRow>[] = useMemo(() => {
    const drill = (row: QualityRow) => {
      if (level === "market") {
        writeParams({
          market: row.market,
          underlying: null,
          expiry: null,
          token: null,
        });
        return;
      }
      if (by === "expiry") {
        if (level === "date") {
          writeParams({ expiry: row.expiry, underlying: null, token: null });
          return;
        }
        if (level === "underlying") {
          writeParams({ underlying: row.underlying, token: null });
          return;
        }
        if (row.token != null) writeParams({ token: String(row.token) });
        return;
      }
      if (level === "underlying") {
        writeParams({
          underlying: row.underlying,
          expiry: null,
          token: null,
        });
        return;
      }
      if (level === "expiry") {
        writeParams({ expiry: row.expiry, token: null });
        return;
      }
      if (row.token != null) writeParams({ token: String(row.token) });
    };

    const cols: Column<QualityRow>[] = [
      {
        key: "name",
        header: "Name",
        locked: true,
        sortValue: (row) => rowLabel(row, level),
        cell: (row) => (
          <button type="button" className="quality-drill" onClick={() => drill(row)}>
            {rowLabel(row, level)}
            {row.no_day_data ? <span className="quality-flag"> no day data</span> : null}
          </button>
        ),
      },
    ];
    if (level === "contract") {
      cols.push(
        {
          key: "type",
          header: "Type",
          sortValue: (row) => row.instrument_type,
          cell: (row) => row.instrument_type ?? "—",
        },
        {
          key: "strike",
          header: "Strike",
          align: "right",
          sortValue: (row) => row.strike,
          cell: (row) =>
            row.strike != null && row.strike !== 0 ? formatCount(row.strike) : "—",
        },
      );
    }
    cols.push(
      {
        key: "session_days",
        header: "Session",
        align: "right",
        sortValue: (row) => row.session_days,
        cell: (row) => formatCount(row.session_days),
      },
      {
        key: "day_bars",
        header: "Day bars",
        align: "right",
        sortValue: (row) => row.day_bars,
        cell: (row) => formatCount(row.day_bars),
      },
      {
        key: "day_gaps",
        header: "Gaps",
        align: "right",
        sortValue: (row) => row.day_gaps,
        cell: (row) => formatCount(row.day_gaps),
      },
      {
        key: "traded_days",
        header: "Traded",
        align: "right",
        sortValue: (row) => row.traded_days,
        cell: (row) => formatCount(row.traded_days),
      },
      {
        key: "minute_days",
        header: "Minute",
        align: "right",
        sortValue: (row) => row.minute_days,
        cell: (row) => formatCount(row.minute_days),
      },
      {
        key: "minute_pct",
        header: "Min %",
        align: "right",
        sortValue: (row) => row.minute_pct,
        cell: (row) => formatPct(row.minute_pct),
      },
      {
        key: "ttq",
        header: "TTQ",
        align: "right",
        sortValue: (row) => row.ttq,
        cell: (row) => formatQty(row.ttq),
      },
      {
        key: "ttv",
        header: "TTV",
        align: "right",
        sortValue: (row) => row.ttv,
        cell: (row) => formatInrCompact(row.ttv),
      },
    );
    return cols;
  }, [level, by, writeParams]);

  function setDrill(next: "security" | "expiry") {
    if (next === by) return;
    writeParams({
      by: next === "security" ? null : "expiry",
      underlying: null,
      expiry: null,
      token: null,
    });
  }

  function commitDays() {
    const text = daysDraft.trim();
    if (!text) {
      writeParams({ days: null });
      return;
    }
    const n = Number(text);
    if (!Number.isInteger(n) || n < 1) {
      setDaysDraft(daysQuery);
      return;
    }
    writeParams({ days: String(n) });
  }

  const crumbs: { label: string; patch: Record<string, string | null> | null }[] = [
    {
      label: "Markets",
      patch: market
        ? { market: null, underlying: null, expiry: null, token: null }
        : null,
    },
  ];
  if (market && by === "expiry") {
    const belowMarket = Boolean(expiry || underlying || token != null);
    crumbs.push({
      label: market,
      patch: belowMarket ? { underlying: null, expiry: null, token: null } : null,
    });
    if (expiry) {
      crumbs.push({
        label: expiry,
        patch: underlying || token != null ? { underlying: null, token: null } : null,
      });
    }
    if (underlying) {
      crumbs.push({
        label: underlying,
        patch: token != null ? { token: null } : null,
      });
    }
  } else if (market) {
    crumbs.push({
      label: market,
      patch: underlying ? { underlying: null, expiry: null, token: null } : null,
    });
    if (underlying) {
      crumbs.push({
        label: underlying,
        patch: expiry ? { expiry: null, token: null } : null,
      });
    }
    if (expiry) {
      crumbs.push({
        label: expiry,
        patch: token != null ? { token: null } : null,
      });
    }
  }
  if (token != null) {
    crumbs.push({
      label: contract?.row.tradingsymbol ?? String(token),
      patch: null,
    });
  }

  const asOfParts = selected
    ? Object.entries(selected.markets)
        .filter(([name]) => !market || name === market)
        .map(([name, meta]) => (market ? meta.as_of ?? "none" : `${name} ${meta.as_of ?? "none"}`))
    : [];

  return (
    <section className="quality-page">
      <div className="page-heading">
        <h1>Quality</h1>
        <p className="page-lead">
          Day and minute coverage from a saved snapshot. Change the window to rescore the last N
          session days. Generate a new snapshot with <span className="mono">uv run ohlcv quality</span>.
        </p>
      </div>

      {error ? <ErrorBanner message={error} /> : null}

      {loadingReports ? (
        <div className="loading">Loading reports…</div>
      ) : reports.length === 0 ? (
        <div className="empty-state">
          <p>No quality snapshots yet. Run: uv run ohlcv quality</p>
          <button type="button" className="btn" onClick={() => void loadReports()}>
            Refresh
          </button>
        </div>
      ) : (
        <>
          <div className="instruments-toolbar">
            <label>
              Snapshot{" "}
              <select
                className="input"
                value={reportId}
                onChange={(e) =>
                  writeParams({
                    report: e.target.value,
                    market: null,
                    underlying: null,
                    expiry: null,
                    token: null,
                  })
                }
              >
                {reports.map((report) => (
                  <option key={report.id} value={report.id}>
                    {report.id} · {formatIst(report.generated_at)}
                  </option>
                ))}
              </select>
            </label>
            <div className="segmented" role="group" aria-label="Session window">
              <button
                type="button"
                className={days == null ? "active" : undefined}
                onClick={() => writeParams({ days: null })}
              >
                All
              </button>
              {DAY_PRESETS.map((preset) => (
                <button
                  key={preset}
                  type="button"
                  className={days === preset ? "active" : undefined}
                  onClick={() => writeParams({ days: String(preset) })}
                >
                  {preset}
                </button>
              ))}
            </div>
            <label>
              Last N{" "}
              <input
                className="input quality-days"
                inputMode="numeric"
                value={daysDraft}
                placeholder="all"
                onFocus={() => setDaysFocused(true)}
                onBlur={() => {
                  setDaysFocused(false);
                  commitDays();
                }}
                onChange={(e) => setDaysDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.currentTarget.blur();
                  }
                }}
              />
            </label>
            {market ? (
              <div className="segmented" role="group" aria-label="Drill">
                <button
                  type="button"
                  className={by === "security" ? "active" : undefined}
                  onClick={() => setDrill("security")}
                >
                  Security
                </button>
                <button
                  type="button"
                  className={by === "expiry" ? "active" : undefined}
                  onClick={() => setDrill("expiry")}
                >
                  Expiry
                </button>
              </div>
            ) : null}
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => void loadReports()}>
              Refresh
            </button>
            <span className="toolbar-meta">
              {selected ? `Generated ${formatIst(selected.generated_at)}` : ""}
              {asOfParts.length > 0 ? ` · As of ${asOfParts.join(" · ")}` : ""}
              {days == null ? " · Window all session days" : ` · Window last ${days} session days`}
            </span>
          </div>

          <nav className="quality-crumbs" aria-label="Drill down">
            {crumbs.map((crumb, index) => (
              <span key={`${crumb.label}-${index}`}>
                {index > 0 ? <span className="quality-crumb-sep"> / </span> : null}
                {crumb.patch ? (
                  <button
                    type="button"
                    className="quality-drill"
                    onClick={() => writeParams(crumb.patch ?? {})}
                  >
                    {crumb.label}
                  </button>
                ) : (
                  <span>{crumb.label}</span>
                )}
              </span>
            ))}
          </nav>

          {!selected || (loadingSummary && !summary) ? (
            <div className="loading">Loading coverage…</div>
          ) : (
            <DataTable
              columns={columns}
              rows={summary?.rows ?? []}
              rowKey={(row) =>
                [
                  row.market,
                  row.underlying ?? "",
                  row.expiry ?? "",
                  row.token ?? "",
                  row.tradingsymbol ?? "",
                ].join(":")
              }
              storageKey="quality-columns-v1"
              defaultSortKey="minute_pct"
              defaultSortDir="asc"
              empty={<div className="empty-state">No rows in this window.</div>}
            />
          )}

          {contract ? (
            <div className="quality-detail">
              <h2>
                {contract.row.tradingsymbol}{" "}
                <span className="quality-flag">
                  {contract.row.instrument_type}
                  {contract.row.no_day_data ? " · no day data" : ""}
                </span>
              </h2>
              <p className="toolbar-meta">
                Session {formatCount(contract.row.session_days)} · Day bars{" "}
                {formatCount(contract.row.day_bars)} · Gaps {formatCount(contract.row.day_gaps)} ·
                Traded {formatCount(contract.row.traded_days)} · Minute{" "}
                {formatCount(contract.row.minute_days)} · {formatPct(contract.row.minute_pct)} · TTQ{" "}
                {formatQty(contract.row.ttq)} · TTV {formatInrCompact(contract.row.ttv)}
              </p>
              <div className="quality-date-grid">
                <div>
                  <h3>Gap dates</h3>
                  {contract.gap_dates.length === 0 ? (
                    <p className="quality-flag">(none)</p>
                  ) : (
                    <ul className="quality-dates">
                      {contract.gap_dates.map((day) => (
                        <li key={day}>{day}</li>
                      ))}
                    </ul>
                  )}
                </div>
                <div>
                  <h3>Traded dates with no minute bar</h3>
                  {contract.traded_without_minute.length === 0 ? (
                    <p className="quality-flag">(none)</p>
                  ) : (
                    <ul className="quality-dates">
                      {contract.traded_without_minute.map((day) => (
                        <li key={day}>{day}</li>
                      ))}
                    </ul>
                  )}
                </div>
              </div>
            </div>
          ) : null}
        </>
      )}
    </section>
  );
}
