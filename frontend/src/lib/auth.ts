import type { AuthUser, LoginResponse } from "../types";

export interface AuthSnapshot {
  status: "signed_out" | "signing_in" | "signed_in";
  user: AuthUser | null;
  expiresAt: string | null;
  version: number;
  notice: string;
}

let snapshot: AuthSnapshot = {
  status: "signed_out", user: null, expiresAt: null, version: 0, notice: "",
};

export interface AuthScope {
  readonly role: AuthUser["role"];
  readonly mode: "platform" | "shop";
  readonly merchant_id: string | null;
  readonly key: string;
}

/** Cache identity only; authorization remains enforced by the server. */
export function getAuthScope(): AuthScope | null {
  if (snapshot.status !== "signed_in" || !snapshot.user) return null;
  const { role, merchant_id } = snapshot.user;
  const mode = role === "service_agent" ? "shop" : "platform";
  if (mode === "shop" && !merchant_id) return null;
  const merchant = mode === "shop" ? merchant_id : null;
  return Object.freeze({ role, mode, merchant_id: merchant, key: JSON.stringify(["scope-v1", role, mode, merchant]) });
}
let token: string | null = null;
let lifetime = new AbortController();
const listeners = new Set<() => void>();

export const getAuthSnapshot = () => snapshot;
export function subscribeAuth(listener: () => void): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

function publish(next: AuthSnapshot) {
  snapshot = next;
  for (const listener of listeners) listener();
}

function replaceLifetime() {
  lifetime.abort();
  lifetime = new AbortController();
  token = null;
}

export function beginLogin(): { version: number; signal: AbortSignal } {
  replaceLifetime();
  publish({ status: "signing_in", user: null, expiresAt: null, version: snapshot.version + 1, notice: "" });
  return { version: snapshot.version, signal: lifetime.signal };
}

export function completeLogin(version: number, result: LoginResponse): boolean {
  if (snapshot.version !== version || snapshot.status !== "signing_in") return false;
  token = result.access_token;
  publish({ ...snapshot, status: "signed_in", user: Object.freeze({ ...result.user }), expiresAt: result.expires_at, notice: "" });
  return true;
}

export function clearAuth(version = snapshot.version, notice = ""): boolean {
  if (snapshot.version !== version) return false;
  replaceLifetime();
  publish({ status: "signed_out", user: null, expiresAt: null, version: version + 1, notice });
  return true;
}

export function invalidateAuth(version: number): boolean {
  if (snapshot.status !== "signed_in") return false;
  return clearAuth(version, "登录已失效，请重新登录。");
}

export function setAuthNotice(version: number, notice: string) {
  if (snapshot.version === version && snapshot.status === "signed_out") publish({ ...snapshot, notice });
}

// The token exists only in this module's memory and request headers, never in storage.
export function getCredentials() {
  if (snapshot.status !== "signed_in" || !token || !snapshot.user) return null;
  return { token, version: snapshot.version, user: snapshot.user, signal: lifetime.signal };
}

export function isCurrentAuth(version: number): boolean {
  return snapshot.status === "signed_in" && snapshot.version === version;
}
