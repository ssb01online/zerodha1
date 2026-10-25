import type { PositionRow } from "./types";

const FO_EXCHANGES = new Set(["NFO", "BFO", "MCX", "CDS"]);
const FO_INSTRUMENT_TYPES = new Set(["CE", "PE", "FUT"]);

export type PositionKind = "all" | "equity" | "fo";
export type ExpiryKey = "all" | "unknown" | string;

export function isFo(row: PositionRow): boolean {
  if (row.segment === "FO") return true;
  if (FO_EXCHANGES.has(row.exchange)) return true;
  if (row.instrument_type && FO_INSTRUMENT_TYPES.has(row.instrument_type)) {
    return true;
  }
  return false;
}

export function filterByKind(
  rows: PositionRow[],
  kind: PositionKind,
): PositionRow[] {
  if (kind === "all") return rows;
  if (kind === "fo") return rows.filter(isFo);
  return rows.filter((r) => !isFo(r));
}

export function filterByExpiry(
  rows: PositionRow[],
  expiryKey: ExpiryKey,
): PositionRow[] {
  if (expiryKey === "all") return rows;
  if (expiryKey === "unknown") {
    return rows.filter((r) => isFo(r) && r.expiry === null);
  }
  return rows.filter((r) => r.expiry === expiryKey);
}

/** Unique expiry keys from F&O rows: ISO dates ascending, plus "unknown" if needed. */
export function collectExpiryKeys(rows: PositionRow[]): ExpiryKey[] {
  const dates = new Set<string>();
  let hasUnknown = false;
  for (const r of rows) {
    if (!isFo(r)) continue;
    if (r.expiry) dates.add(r.expiry);
    else hasUnknown = true;
  }
  const keys: ExpiryKey[] = [...dates].sort();
  if (hasUnknown) keys.push("unknown");
  return keys;
}
