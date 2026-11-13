// Thin client for the console's JSON API (src/ouroboros/gui/api.py).
// Every call carries the per-launch token the server writes into the page.

const params = new URLSearchParams(window.location.search);

function readToken(): string {
  const meta = document.querySelector<HTMLMetaElement>('meta[name="ouroboros-token"]');
  return meta?.content || params.get("token") || "";
}

const TOKEN = readToken();

export class ApiFailure extends Error {
  status: number;
  code: string;
  extra: Record<string, unknown>;

  constructor(status: number, code: string, message: string, extra: Record<string, unknown> = {}) {
    super(message);
    this.name = "ApiFailure";
    this.status = status;
    this.code = code;
    this.extra = extra;
  }
}

type Method = "GET" | "POST" | "PUT" | "DELETE";

export async function call<T>(method: Method, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { "X-Ouroboros-Token": TOKEN };
  const init: RequestInit = { method, headers, cache: "no-store" };
  if (method === "POST" || method === "PUT") {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body ?? {});
  }
  let response: Response;
  try {
    response = await fetch(path, init);
  } catch {
    throw new ApiFailure(0, "offline", "The console is not responding.");
  }
  let payload: unknown = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok) {
    const data = (payload && typeof payload === "object" ? payload : {}) as Record<string, unknown>;
    const { error, code, ...extra } = data;
    throw new ApiFailure(
      response.status,
      typeof code === "string" ? code : "error",
      typeof error === "string" ? error : `HTTP ${response.status}`,
      extra,
    );
  }
  return payload as T;
}

export const get = <T>(path: string) => call<T>("GET", path);
export const post = <T>(path: string, body?: unknown) => call<T>("POST", path, body);
export const put = <T>(path: string, body?: unknown) => call<T>("PUT", path, body);
export const del = <T>(path: string) => call<T>("DELETE", path);

/** EventSource cannot set headers, so streams pass the token in the query. */
export function streamUrl(path: string): string {
  const joiner = path.includes("?") ? "&" : "?";
  return `${path}${joiner}token=${encodeURIComponent(TOKEN)}`;
}

export interface Healthz {
  status: string;
  service: string;
  version: string;
  uptime: number;
  pid: number;
  port: number;
}

export async function healthz(): Promise<Healthz | null> {
  try {
    const response = await fetch("/healthz", { cache: "no-store" });
    if (!response.ok) return null;
    return (await response.json()) as Healthz;
  } catch {
    return null;
  }
}
