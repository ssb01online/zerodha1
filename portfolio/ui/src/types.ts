/** Types mirroring portfolio.serialize / PortfolioService payloads. */

export interface BookTotals {
  invested: number;
  current_value: number;
  pnl: number;
  pnl_pct: number | null;
  day_change: number;
}

export interface PositionsSummary {
  open_count: number;
  pnl: number;
  m2m: number;
}

export interface Summary {
  equity: BookTotals;
  mf: BookTotals;
  combined: BookTotals;
  positions: PositionsSummary;
}

export interface HoldingRow {
  isin: string;
  exchange: string;
  tradingsymbol: string;
  instrument_token: number;
  product: string;
  quantity: number;
  t1_quantity: number;
  collateral_quantity: number;
  used_quantity: number;
  opening_quantity: number;
  owned_quantity: number;
  average_price: number;
  last_price: number;
  close_price: number;
  pnl: number;
  day_change: number;
  day_change_percentage: number;
  invested: number;
  current_value: number;
  pnl_pct: number | null;
  day_change_value: number;
  weight: number | null;
}

export interface MfRow {
  isin: string;
  folio: string;
  fund: string;
  tradingsymbol: string;
  quantity: number;
  average_price: number;
  last_price: number;
  pnl: number;
  invested: number;
  current_value: number;
  pnl_pct: number | null;
}

export interface PositionRow {
  exchange: string;
  tradingsymbol: string;
  instrument_token: number;
  product: string;
  segment: string;
  quantity: number;
  overnight_quantity: number;
  multiplier: number;
  average_price: number;
  last_price: number;
  close_price: number;
  value: number;
  pnl: number;
  m2m: number;
  realised: number;
  unrealised: number;
  buy_quantity: number;
  buy_price: number;
  buy_value: number;
  sell_quantity: number;
  sell_price: number;
  sell_value: number;
  day_buy_quantity: number;
  day_buy_price: number;
  day_buy_value: number;
  day_sell_quantity: number;
  day_sell_price: number;
  day_sell_value: number;
  is_open: boolean;
}

export interface PortfolioUser {
  user_id: string | null;
  user_name: string | null;
}

export interface PortfolioPayload {
  as_of: string | null;
  source: string;
  user: PortfolioUser;
  summary: Summary;
  holdings: HoldingRow[];
  mf_holdings: MfRow[];
  positions: {
    net: PositionRow[];
    day: PositionRow[];
  };
  cached?: boolean;
  fetched_at?: string | null;
  snapshot_id?: number;
  observed_at?: string | null;
  effective_date?: string | null;
  fingerprint_hash?: string;
  composition?: Record<string, number>;
}

export interface CompositionDiff {
  added: Record<string, number>;
  removed: Record<string, number>;
  changed: Record<string, { old: number; new: number }>;
  empty: boolean;
  summary: string;
}

export interface CaptureResult {
  saved: boolean;
  snapshot_id: number | null;
  fingerprint_hash: string;
  observed_at: string | null;
  diff: CompositionDiff;
}

export interface SnapshotCounts {
  holdings: number;
  positions_net: number;
  positions_day: number;
  mf_holdings: number;
}

export interface SnapshotMeta {
  id: number;
  observed_at: string | null;
  effective_date: string | null;
  source: string;
  fingerprint_hash: string;
  counts: SnapshotCounts;
}

export interface SnapshotsList {
  snapshots: SnapshotMeta[];
  count: number;
}

export interface HealthSession {
  valid: boolean;
  user_id: string | null;
  expires_at: string | null;
  message: string | null;
}

export interface HealthDb {
  path: string;
  exists: boolean;
  schema_version: number | null;
  expected_schema_version: number;
  snapshot_count: number;
}

export interface Health {
  ok: boolean;
  session: HealthSession;
  db: HealthDb;
}

export interface ApiErrorBody {
  error: string;
  message: string;
  steps?: string[];
}
