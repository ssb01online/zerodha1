import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, getSnapshot } from "../api";
import ErrorBanner from "../components/ErrorBanner";
import PortfolioView from "../components/PortfolioView";
import TokenErrorPanel from "../components/TokenErrorPanel";
import { formatIst, formatUnits } from "../format";
import type { PortfolioPayload } from "../types";

export default function SnapshotPage() {
  const { id: idParam } = useParams();
  const id = Number(idParam);

  const [data, setData] = useState<PortfolioPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ApiError | Error | null>(null);

  const load = useCallback(async () => {
    if (!Number.isFinite(id) || id <= 0) {
      setError(new Error("Invalid snapshot id"));
      setLoading(false);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const payload = await getSnapshot(id);
      setData(payload);
    } catch (err) {
      setData(null);
      setError(
        err instanceof ApiError || err instanceof Error
          ? err
          : new Error(String(err)),
      );
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  if (error instanceof ApiError && error.isTokenInvalid) {
    return <TokenErrorPanel error={error} onRetry={() => void load()} />;
  }

  const composition = data?.composition
    ? Object.entries(data.composition).sort(([a], [b]) => a.localeCompare(b))
    : [];

  return (
    <div>
      <div className="page-header">
        <div>
          <h1>Snapshot #{Number.isFinite(id) ? id : "—"}</h1>
          <p className="page-sub">Point-in-time composition and prices as captured.</p>
        </div>
        <Link to="/snapshots" className="btn">
          All snapshots
        </Link>
      </div>

      {error ? (
        error instanceof ApiError && error.error === "snapshot_not_found" ? (
          <ErrorBanner
            title="Snapshot not found"
            message={error.message}
            onRetry={() => void load()}
          />
        ) : (
          <ErrorBanner message={error.message} onRetry={() => void load()} />
        )
      ) : null}

      {loading && !data ? (
        <div className="loading">Loading snapshot…</div>
      ) : null}

      {data ? (
        <>
          <div className="asof-banner">
            <span>
              As of <strong>{formatIst(data.observed_at ?? data.as_of)}</strong>{" "}
              (snapshot #{data.snapshot_id}) — prices shown are as captured.
              {data.effective_date ? (
                <>
                  {" "}
                  Effective date: {formatIst(data.effective_date, "date")}.
                </>
              ) : null}
            </span>
            <Link to="/portfolio">Back to Live</Link>
          </div>
          <PortfolioView data={data} />
          {composition.length > 0 ? (
            <details className="composition-details">
              <summary>Composition ({composition.length} keys)</summary>
              <ul className="composition-list">
                {composition.map(([key, qty]) => (
                  <li key={key}>
                    <span>{key}</span>
                    <span>{formatUnits(qty)}</span>
                  </li>
                ))}
              </ul>
            </details>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
