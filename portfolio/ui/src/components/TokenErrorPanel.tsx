import type { ApiError } from "../api";

interface TokenErrorPanelProps {
  error: ApiError;
  onRetry: () => void;
}

export default function TokenErrorPanel({
  error,
  onRetry,
}: TokenErrorPanelProps) {
  const steps =
    error.steps && error.steps.length > 0
      ? error.steps
      : [
          "Open a terminal in the project root.",
          "Run: uv run kitetools login",
          "Finish the browser login (Zerodha credentials + TOTP).",
          "Reload this page.",
        ];

  return (
    <div className="token-panel">
      <h2>Session token invalid</h2>
      <p>{error.message}</p>
      <ol>
        {steps.map((step) => (
          <li key={step}>
            {step.includes("uv run") ? (
              <>
                {step.split(/(uv run[^\s.]*(?:\s+[^\s.]+)*)/).map((part, i) =>
                  part.startsWith("uv run") ? (
                    <code key={i}>{part}</code>
                  ) : (
                    <span key={i}>{part}</span>
                  ),
                )}
              </>
            ) : (
              step
            )}
          </li>
        ))}
      </ol>
      <button type="button" className="btn btn-primary" onClick={onRetry}>
        Retry
      </button>
    </div>
  );
}
