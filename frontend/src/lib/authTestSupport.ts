import { beginLogin, completeLogin } from "./auth";
import type { LoginResponse } from "../types";

export function loginResult(userId = "buyer-a", role: LoginResponse["user"]["role"] = "buyer", merchantId: string | null = role === "service_agent" ? "m_demo" : null): LoginResponse {
  return {
    access_token: `test-token-${userId}`, token_type: "bearer", expires_in: 3600,
    expires_at: new Date(Date.now() + 3600000).toISOString(),
    user: { user_id: userId, role, merchant_id: merchantId, status: "active" },
  };
}

export function signIn(userId = "buyer-a", role: LoginResponse["user"]["role"] = "buyer", merchantId: string | null = role === "service_agent" ? "m_demo" : null) {
  const attempt = beginLogin();
  completeLogin(attempt.version, loginResult(userId, role, merchantId));
  return attempt;
}

export class MemoryStorage {
  private values = new Map<string, string>();
  get length() { return this.values.size; }
  getItem(key: string) { return this.values.get(key) ?? null; }
  setItem(key: string, value: string) { this.values.set(key, String(value)); }
  removeItem(key: string) { this.values.delete(key); }
  clear() { this.values.clear(); }
  key(index: number) { return Array.from(this.values.keys())[index] ?? null; }
}
