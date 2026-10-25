import { formatInr, formatInrCompact, formatPct, pnlClass } from "../format";
import type { Summary } from "../types";

interface SummaryCardsProps {
  summary: Summary;
}

export default function SummaryCards({ summary }: SummaryCardsProps) {
  const { combined, equity, mf, positions } = summary;

  return (
    <div className="summary-grid">
      <div className="summary-card">
        <div className="label">Current value</div>
        <div className="value">{formatInrCompact(combined.current_value)}</div>
        <div className="sub">{formatInr(combined.current_value)}</div>
      </div>
      <div className="summary-card">
        <div className="label">Invested</div>
        <div className="value">{formatInrCompact(combined.invested)}</div>
        <div className="sub">{formatInr(combined.invested)}</div>
      </div>
      <div className="summary-card">
        <div className="label">Total P&amp;L</div>
        <div className={`value ${pnlClass(combined.pnl)}`}>
          {formatInrCompact(combined.pnl)}
        </div>
        <div className={`sub ${pnlClass(combined.pnl)}`}>
          {formatPct(combined.pnl_pct)}
        </div>
      </div>
      <div className="summary-card">
        <div className="label">Day change</div>
        <div className={`value ${pnlClass(equity.day_change)}`}>
          {formatInrCompact(equity.day_change)}
        </div>
        <div className="sub">Equity only</div>
      </div>
      <div className="summary-card">
        <div className="label">Mutual funds</div>
        <div className="value">{formatInrCompact(mf.current_value)}</div>
        <div className={`sub ${pnlClass(mf.pnl)}`}>
          P&amp;L {formatInr(mf.pnl)} ({formatPct(mf.pnl_pct)})
        </div>
      </div>
      <div className="summary-card">
        <div className="label">Open positions</div>
        <div className="value">{positions.open_count}</div>
        <div className={`sub ${pnlClass(positions.pnl)}`}>
          P&amp;L {formatInr(positions.pnl)}
        </div>
      </div>
    </div>
  );
}
