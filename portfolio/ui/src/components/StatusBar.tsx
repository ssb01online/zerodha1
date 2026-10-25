import { formatIst } from "../format";
import type { CaptureResult, PortfolioPayload } from "../types";

interface StatusBarProps {
  data: PortfolioPayload;
  capture: CaptureResult | null;
  captureError: string | null;
  capturing: boolean;
}

export default function StatusBar({
  data,
  capture,
  captureError,
  capturing,
}: StatusBarProps) {
  return (
    <div className="status-bar">
      <span>
        <span className="tag">Live</span>{" "}
        {formatIst(data.fetched_at ?? data.as_of)}
        {data.cached ? <span className="tag" style={{ marginLeft: 6 }}>cached</span> : null}
      </span>
      {capturing ? (
        <span className="status-muted">Capturing…</span>
      ) : captureError ? (
        <span className="status-warn">Capture: {captureError}</span>
      ) : capture ? (
        capture.saved ? (
          <span className="status-ok">
            Snapshot #{capture.snapshot_id} saved
            {capture.diff.summary && !capture.diff.empty
              ? ` — ${capture.diff.summary}`
              : null}
          </span>
        ) : (
          <span className="status-muted">No composition change</span>
        )
      ) : null}
      {data.user.user_name ? (
        <span className="status-muted">{data.user.user_name}</span>
      ) : null}
    </div>
  );
}
