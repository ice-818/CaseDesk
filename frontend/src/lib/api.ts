import type { Capabilities, ChatRequest, LoginResponse, StreamEvent } from "../types";
import { getCredentials, invalidateAuth, isCurrentAuth } from "./auth";
import { SseDecoder } from "./sse";

const API_BASE = import.meta.env.VITE_API_BASE ?? "";
type Credentials = NonNullable<ReturnType<typeof getCredentials>>;

export class ApiError extends Error {
  constructor(public readonly status: number, message: string) { super(message); }
}

function apiUrl(path: string): string {
  if (!path.startsWith("/") || path.startsWith("//") || path.includes("\\")) throw new Error("无效的 CaseDesk API 路径");
  const url = new URL(`/api/v1${path}`, "http://casedesk.local");
  if (!url.pathname.startsWith("/api/v1/")) throw new Error("无效的 CaseDesk API 路径");
  for (const name of url.searchParams.keys()) {
    if (/^(?:access_token|token|authorization)$/i.test(name)) throw new Error("凭证不得出现在 URL 中");
  }
  return `${API_BASE}${url.pathname}${url.search}`;
}

function requireCredentials(): Credentials {
  const credentials = getCredentials();
  if (!credentials) throw new ApiError(401, "请先登录。");
  return credentials;
}

function checkCurrent(credentials: Credentials, signal?: AbortSignal) {
  signal?.throwIfAborted();
  if (!isCurrentAuth(credentials.version)) throw new DOMException("登录身份已改变", "AbortError");
}

function statusError(status: number, version?: number, message?: string): ApiError {
  if (status === 401) {
    if (version !== undefined) invalidateAuth(version);
    return new ApiError(401, "登录已失效，请重新登录。");
  }
  if (status === 403) return new ApiError(403, "权限不足，无法执行此操作。");
  return new ApiError(status, status >= 500 ? "服务暂时不可用，请稍后重试。" : message || `请求失败（HTTP ${status}）`);
}

async function ensureOk(response: Response, version?: number) {
  if (response.ok) return;
  if (response.status === 401 || response.status === 403) throw statusError(response.status, version);
  let message: string | undefined;
  try {
    const value: unknown = await response.json();
    if (typeof value === "object" && value !== null && "message" in value && typeof value.message === "string") message = value.message;
  } catch { /* An empty error response still has an HTTP status. */ }
  throw statusError(response.status, version, message);
}

async function authenticatedFetch(path: string, body?: unknown, signal?: AbortSignal, accept = "application/json") {
  const credentials = requireCredentials();
  const combined = signal ? AbortSignal.any([signal, credentials.signal]) : credentials.signal;
  const response = await fetch(apiUrl(path), {
    method: body === undefined ? "GET" : "POST",
    headers: { Authorization: `Bearer ${credentials.token}`, Accept: accept, ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: combined, cache: "no-store",
  });
  await ensureOk(response, credentials.version);
  checkCurrent(credentials, combined);
  return { response, credentials, signal: combined };
}

/** CaseDesk requests only. Failed writes are never automatically replayed. */
export async function apiRequest<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const result = await authenticatedFetch(path, body, signal);
  const value: unknown = result.response.status === 204 ? undefined : await result.response.json();
  checkCurrent(result.credentials, result.signal);
  return value as T;
}

export async function loginRequest(userId: string, password: string, signal?: AbortSignal): Promise<LoginResponse> {
  const response = await fetch(apiUrl("/auth/login"), {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_id: userId, password }), signal, cache: "no-store",
  });
  if (!response.ok) throw new Error("登录失败，请检查账号和密码或稍后重试。");
  const result = await response.json() as LoginResponse;
  if (!result.access_token || result.token_type !== "bearer" || !result.user?.user_id ||
      !["buyer", "service_agent", "operator"].includes(result.user.role) ||
      !Number.isFinite(Date.parse(result.expires_at))) throw new Error("登录失败，请稍后重试。");
  return result;
}

export async function logoutRequest(credentials: Credentials): Promise<void> {
  const response = await fetch(apiUrl("/auth/logout"), {
    method: "POST", headers: { Authorization: `Bearer ${credentials.token}` },
    signal: AbortSignal.timeout(15000), cache: "no-store",
  });
  if (response.status !== 401) await ensureOk(response);
}

export async function checkHealth(signal?: AbortSignal): Promise<boolean> {
  try { await apiRequest("/health", undefined, signal); return true; }
  catch { return false; }
}

export async function getCapabilities(signal?: AbortSignal): Promise<Capabilities | null> {
  try { return await apiRequest<Capabilities>("/capabilities", undefined, signal); }
  catch { return null; }
}

export async function streamChat(payload: ChatRequest, onEvent: (event: StreamEvent) => void, signal: AbortSignal): Promise<void> {
  const result = await authenticatedFetch("/chat/stream", payload, signal, "text/event-stream");
  checkCurrent(result.credentials, result.signal);
  if (!result.response.body) throw new Error("浏览器不支持流式响应");
  const reader = result.response.body.getReader();
  const decoder = new SseDecoder();
  let ended = false;
  function deliver(events: StreamEvent[]) {
    for (const event of events) {
      checkCurrent(result.credentials, result.signal);
      if (event.event === "error") throw statusError(event.status_code ?? 500, result.credentials.version, event.message);
      onEvent(event);
    }
  }
  const cancel = () => { void reader.cancel().catch(() => {}); };
  result.signal.addEventListener("abort", cancel, { once: true });
  try {
    while (true) {
      const { done, value } = await reader.read();
      checkCurrent(result.credentials, result.signal);
      if (done) { ended = true; break; }
      deliver(decoder.feed(value));
    }
    deliver(decoder.finish());
  } finally {
    result.signal.removeEventListener("abort", cancel);
    if (!ended) void reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
