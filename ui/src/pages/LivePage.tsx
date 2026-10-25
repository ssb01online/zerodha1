import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, capture, getLive } from "../api";
import ErrorBanner from "../components/ErrorBanner";
import PortfolioView from "../components/PortfolioView";
import StatusBar from "../components/StatusBar";
import TokenErrorPanel from "../components/TokenErrorPanel";
import type { CaptureResult, PortfolioPayload } from "../types";

export default function LivePage() {
  const [data, setData] = useState<PortfolioPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ApiError | Error | null>(null);
  const [captureResult, setCaptureResult] = useState<CaptureResult | null>(
    null,
  );
  const [captureError, setCaptureError] = useState<string | null>(null);
  const [capturing, setCapturing] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  // Guard StrictMode double-mount so auto-capture runs once per page load.
  const autoCaptureDone = useRef(false);

  const runCapture = useCallback(async () => {
    setCapturing(true);
    setCaptureError(null);
    try {
      const result = await capture();
      setCaptureResult(result);
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : "Capture failed";
      setCaptureError(msg);
      setCaptureResult(null);
    } finally {
      setCapturing(false);
    }
  }, []);

  const load = useCallback(
    async (refresh: boolean, doAutoCapture: boolean) => {
      setError(null);
      if (refresh) setRefreshing(true);
      else setLoading(true);
      try {
        const payload = await getLive(refresh);
        setData(payload);
        if (doAutoCapture) {
          await runCapture();
        }
      } catch (err) {
        setData(null);
        setError(
          err instanceof ApiError || err instanceof Error
            ? err
            : new Error(String(err)),
        );
      } finally {
        setLoading(false);
        setRefreshing(false);
      }
    },
    [runCapture],
  );

  useEffect(() => {
    const shouldCapture = !autoCaptureDone.current;
    if (shouldCapture) {
      autoCaptureDone.current = true;
    }
    void load(false, shouldCapture);
  }, [load]);

  if (error instanceof ApiError && error.isTokenInvalid) {
    return (
      <TokenErrorPanel
        error={error}
        onRetry={() => {
          autoCaptureDone.current = false;
          void load(true, true).then(() => {
            autoCaptureDone.current = true;
          });
        }}
      />
    );
  }

  return (
    <div>
      <div className="page-header">
        <div>
          <h1>Live portfolio</h1>
          <p className="page-sub">
            Holdings, mutual funds, and positions from Kite. Snapshots are
            saved automatically when quantities change.
          </p>
        </div>
        <button
          type="button"
          className="btn"
          disabled={loading || refreshing || capturing}
          onClick={() => void load(true, true)}
        >
          {refreshing ? "Refreshing…" : "Refresh"}
        </button>
      </div>

      {error && !(error instanceof ApiError && error.isTokenInvalid) ? (
        <ErrorBanner
          message={error.message}
          onRetry={() => void load(true, true)}
        />
      ) : null}

      {loading && !data ? (
        <div className="loading">Loading portfolio…</div>
      ) : null}

      {data ? (
        <>
          <StatusBar
            data={data}
            capture={captureResult}
            captureError={captureError}
            capturing={capturing}
          />
          <PortfolioView data={data} />
        </>
      ) : null}
    </div>
  );
}
