import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, listSnapshots } from "../api";
import DataTable, { type Column } from "../components/DataTable";
import ErrorBanner from "../components/ErrorBanner";
import TokenErrorPanel from "../components/TokenErrorPanel";
import { formatIst } from "../format";
import type { SnapshotMeta } from "../types";

export default function SnapshotsPage() {
  const [rows, setRows] = useState<SnapshotMeta[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ApiError | Error | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await listSnapshots(100);
      setRows(res.snapshots);
    } catch (err) {
      setRows([]);
      setError(
        err instanceof ApiError || err instanceof Error
          ? err
          : new Error(String(err)),
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (error instanceof ApiError && error.isTokenInvalid) {
    return <TokenErrorPanel error={error} onRetry={() => void load()} />;
  }

  const columns: Column<SnapshotMeta>[] = [
    {
      key: "id",
      header: "ID",
      cell: (r) => (
        <Link to={`/snapshots/${r.id}`}>#{r.id}</Link>
      ),
    },
    {
      key: "observed",
      header: "Observed at",
      cell: (r) => formatIst(r.observed_at),
    },
    {
      key: "effective",
      header: "Effective date",
      cell: (r) =>
        r.effective_date ? formatIst(r.effective_date, "date") : "—",
    },
    {
      key: "source",
      header: "Source",
      cell: (r) => r.source,
    },
    {
      key: "counts",
      header: "Counts",
      cell: (r) =>
        `EQ ${r.counts.holdings} · MF ${r.counts.mf_holdings} · POS ${r.counts.positions_net}`,
    },
  ];

  return (
    <div>
      <div className="page-header">
        <div>
          <h1>Snapshots</h1>
          <p className="page-sub">
            Sparse composition checkpoints — created when holdings, mutual
            funds, or position quantities change.
          </p>
        </div>
        <button
          type="button"
          className="btn"
          disabled={loading}
          onClick={() => void load()}
        >
          Refresh
        </button>
      </div>

      {error && !(error instanceof ApiError && error.isTokenInvalid) ? (
        <ErrorBanner
          message={error.message}
          onRetry={() => void load()}
        />
      ) : null}

      {loading && rows.length === 0 ? (
        <div className="loading">Loading snapshots…</div>
      ) : (
        <DataTable
          columns={columns}
          rows={rows}
          rowKey={(r) => String(r.id)}
          empty={
            <div className="empty-state">
              No snapshots yet. Open the Live page — a snapshot is saved
              automatically when quantities change.
            </div>
          }
        />
      )}
    </div>
  );
}
