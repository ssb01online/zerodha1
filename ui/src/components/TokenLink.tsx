import type { MouseEvent } from "react";
import { Link } from "react-router-dom";

interface TokenLinkProps {
  token: number;
  label?: string;
  className?: string;
}

/** Navigates to instrument detail; copy button keeps the token handy for later APIs. */
export default function TokenLink({
  token,
  label,
  className,
}: TokenLinkProps) {
  const text = label ?? String(token);

  async function copy(e: MouseEvent) {
    e.preventDefault();
    e.stopPropagation();
    try {
      await navigator.clipboard.writeText(String(token));
    } catch {
      /* ignore */
    }
  }

  return (
    <span className={`token-cell${className ? ` ${className}` : ""}`}>
      <Link to={`/instruments/token/${token}`} className="token-link mono">
        {text}
      </Link>
      <button
        type="button"
        className="btn btn-ghost btn-sm token-copy"
        onClick={copy}
        title="Copy instrument token"
        aria-label={`Copy token ${token}`}
      >
        Copy
      </button>
    </span>
  );
}
