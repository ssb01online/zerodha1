import type {
  ApiErrorBody,
  CaptureResult,
  Health,
  InstrumentRow,
  InstrumentsMeta,
  InstrumentsSearchResult,
  NfoExpiriesResult,
  NfoUnderlyingsResult,
  OptionChainResult,
  PortfolioPayload,
  PreopenDayPayload,
  PreopenDaysResult,
  QualityContract,
  QualityReportMeta,
  QualityReportsResult,
  QualitySummary,
  SnapshotsList,
} from "./types";

export class ApiError extends Error {
  status: number;
  error: string;
  steps?: string[];

  constructor(status: number, body: ApiErrorBody) {
    super(body.message);
    this.name = "ApiError";
    this.status = status;
    this.error = body.error;
    this.steps = body.steps;
  }

  get isTokenInvalid(): boolean {
    return this.status === 401 || this.error === "token_invalid";
  }
}

export async function apiFetch<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(init?.headers ?? {}),
    },
  });

  let body: unknown = null;
  const text = await res.text();
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = { error: "parse_error", message: text };
    }
  }

  if (!res.ok) {
    const errBody = (body ?? {
      error: "http_error",
      message: res.statusText || `HTTP ${res.status}`,
    }) as ApiErrorBody;
    throw new ApiError(res.status, errBody);
  }

  return body as T;
}

export function getHealth(): Promise<Health> {
  return apiFetch<Health>("/api/health");
}

export function getLive(refresh = false): Promise<PortfolioPayload> {
  const q = refresh ? "?refresh=true" : "";
  return apiFetch<PortfolioPayload>(`/api/portfolio/live${q}`);
}

export function capture(): Promise<CaptureResult> {
  return apiFetch<CaptureResult>("/api/portfolio/capture", { method: "POST" });
}

export function listSnapshots(limit = 50): Promise<SnapshotsList> {
  return apiFetch<SnapshotsList>(`/api/portfolio/snapshots?limit=${limit}`);
}

export function getSnapshot(id: number): Promise<PortfolioPayload> {
  return apiFetch<PortfolioPayload>(`/api/portfolio/snapshots/${id}`);
}

export function getInstrumentsMeta(): Promise<InstrumentsMeta> {
  return apiFetch<InstrumentsMeta>("/api/instruments/meta");
}

export function searchInstruments(params: {
  q?: string;
  exchange?: string;
  instrument_type?: string;
  limit?: number;
  offset?: number;
}): Promise<InstrumentsSearchResult> {
  const sp = new URLSearchParams();
  if (params.q) sp.set("q", params.q);
  if (params.exchange) sp.set("exchange", params.exchange);
  if (params.instrument_type) sp.set("instrument_type", params.instrument_type);
  sp.set("limit", String(params.limit ?? 50));
  sp.set("offset", String(params.offset ?? 0));
  return apiFetch<InstrumentsSearchResult>(
    `/api/instruments/search?${sp.toString()}`,
  );
}

export function getInstrumentByToken(token: number): Promise<InstrumentRow> {
  return apiFetch<InstrumentRow>(`/api/instruments/by-token/${token}`);
}

export function listNfoUnderlyings(params?: {
  q?: string;
  limit?: number;
}): Promise<NfoUnderlyingsResult> {
  const sp = new URLSearchParams();
  if (params?.q) sp.set("q", params.q);
  sp.set("limit", String(params?.limit ?? 100));
  return apiFetch<NfoUnderlyingsResult>(
    `/api/instruments/nfo/underlyings?${sp.toString()}`,
  );
}

export function listNfoExpiries(underlying: string): Promise<NfoExpiriesResult> {
  const sp = new URLSearchParams({ underlying });
  return apiFetch<NfoExpiriesResult>(
    `/api/instruments/nfo/expiries?${sp.toString()}`,
  );
}

export function getNfoChain(
  underlying: string,
  expiry: string,
): Promise<OptionChainResult> {
  const sp = new URLSearchParams({ underlying, expiry });
  return apiFetch<OptionChainResult>(
    `/api/instruments/nfo/chain?${sp.toString()}`,
  );
}

export function listQualityReports(): Promise<QualityReportsResult> {
  return apiFetch<QualityReportsResult>("/api/ohlcv/quality/reports");
}

export function getQualityReport(id: string): Promise<QualityReportMeta> {
  return apiFetch<QualityReportMeta>(`/api/ohlcv/quality/reports/${id}`);
}

export function getQualitySummary(
  id: string,
  params?: {
    days?: number | null;
    market?: string;
    underlying?: string;
    expiry?: string;
    by?: "security" | "expiry";
  },
): Promise<QualitySummary> {
  const sp = new URLSearchParams();
  if (params?.days != null) sp.set("days", String(params.days));
  if (params?.market) sp.set("market", params.market);
  if (params?.underlying) sp.set("underlying", params.underlying);
  if (params?.expiry) sp.set("expiry", params.expiry);
  if (params?.by === "expiry") sp.set("by", "expiry");
  const q = sp.toString();
  return apiFetch<QualitySummary>(
    `/api/ohlcv/quality/reports/${id}/summary${q ? `?${q}` : ""}`,
  );
}

export function getQualityContract(
  id: string,
  token: number,
  params?: { days?: number | null; market?: string },
): Promise<QualityContract> {
  const sp = new URLSearchParams();
  sp.set("token", String(token));
  if (params?.days != null) sp.set("days", String(params.days));
  if (params?.market) sp.set("market", params.market);
  return apiFetch<QualityContract>(
    `/api/ohlcv/quality/reports/${id}/contract?${sp.toString()}`,
  );
}

export function listPreopenDays(): Promise<PreopenDaysResult> {
  return apiFetch<PreopenDaysResult>("/api/preopen/days");
}

export function getPreopenDay(
  ymd: string,
  params?: {
    limit?: number;
    min_value?: number;
    exchange?: string;
  },
): Promise<PreopenDayPayload> {
  const sp = new URLSearchParams();
  sp.set("limit", String(params?.limit ?? 50));
  if (params?.min_value != null) sp.set("min_value", String(params.min_value));
  if (params?.exchange) sp.set("exchange", params.exchange);
  return apiFetch<PreopenDayPayload>(
    `/api/preopen/day/${ymd}?${sp.toString()}`,
  );
}
