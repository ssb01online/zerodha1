import { useEffect, useState } from "react";
import type { PortfolioPayload } from "../types";
import HoldingsTable from "./HoldingsTable";
import MfTable from "./MfTable";
import PositionsTable from "./PositionsTable";
import SummaryCards from "./SummaryCards";
import Tabs from "./Tabs";

const TAB_KEY = "portfolio_ui_tab_v1";

type TabId = "holdings" | "mf" | "positions";

function readTab(): TabId {
  const t = localStorage.getItem(TAB_KEY);
  if (t === "holdings" || t === "mf" || t === "positions") return t;
  return "holdings";
}

interface PortfolioViewProps {
  data: PortfolioPayload;
}

export default function PortfolioView({ data }: PortfolioViewProps) {
  const [tab, setTab] = useState<TabId>(readTab);

  useEffect(() => {
    localStorage.setItem(TAB_KEY, tab);
  }, [tab]);

  const openPositions = data.positions.net.filter((p) => p.is_open).length;

  return (
    <div>
      <SummaryCards summary={data.summary} />
      <Tabs
        active={tab}
        onChange={(id) => setTab(id as TabId)}
        tabs={[
          { id: "holdings", label: "Holdings", count: data.holdings.length },
          {
            id: "mf",
            label: "Mutual funds",
            count: data.mf_holdings.length,
          },
          {
            id: "positions",
            label: "Positions",
            count: openPositions,
          },
        ]}
      />
      {tab === "holdings" ? <HoldingsTable rows={data.holdings} /> : null}
      {tab === "mf" ? <MfTable rows={data.mf_holdings} /> : null}
      {tab === "positions" ? (
        <PositionsTable
          net={data.positions.net}
          day={data.positions.day}
        />
      ) : null}
    </div>
  );
}
