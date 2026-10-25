const inr = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  maximumFractionDigits: 2,
});

const inrWhole = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  maximumFractionDigits: 0,
});

const qtyFmt = new Intl.NumberFormat("en-IN", {
  maximumFractionDigits: 0,
});

const unitsFmt = new Intl.NumberFormat("en-IN", {
  minimumFractionDigits: 0,
  maximumFractionDigits: 3,
});

const pctFmt = new Intl.NumberFormat("en-IN", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
  signDisplay: "exceptZero",
});

const priceFmt = new Intl.NumberFormat("en-IN", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const istDateTime = new Intl.DateTimeFormat("en-IN", {
  timeZone: "Asia/Kolkata",
  year: "numeric",
  month: "short",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

const istDate = new Intl.DateTimeFormat("en-IN", {
  timeZone: "Asia/Kolkata",
  year: "numeric",
  month: "short",
  day: "2-digit",
});

export function formatInr(value: number, whole = false): string {
  return (whole ? inrWhole : inr).format(value);
}

/** Compact INR for summary cards (e.g. ₹12.34 L, ₹1.23 Cr). */
export function formatInrCompact(value: number): string {
  const abs = Math.abs(value);
  const sign = value < 0 ? "-" : "";
  if (abs >= 1e7) {
    return `${sign}₹${(abs / 1e7).toFixed(2)} Cr`;
  }
  if (abs >= 1e5) {
    return `${sign}₹${(abs / 1e5).toFixed(2)} L`;
  }
  return formatInr(value);
}

export function formatQty(value: number): string {
  return qtyFmt.format(value);
}

export function formatUnits(value: number): string {
  return unitsFmt.format(value);
}

export function formatPrice(value: number): string {
  return priceFmt.format(value);
}

export function formatPct(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "—";
  }
  return `${pctFmt.format(value)}%`;
}

export function formatIst(
  iso: string | null | undefined,
  mode: "datetime" | "date" = "datetime",
): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return (mode === "date" ? istDate : istDateTime).format(d) + " IST";
}

export function pnlClass(value: number | null | undefined): string {
  if (value === null || value === undefined || value === 0) return "pnl-flat";
  return value > 0 ? "pnl-pos" : "pnl-neg";
}
