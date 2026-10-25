import type {
  ApiErrorBody,
  CaptureResult,
  Health,
  PortfolioPayload,
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
