import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { beginLogin, clearAuth, completeLogin, getAuthSnapshot, getAuthScope, getCredentials, invalidateAuth, subscribeAuth } from "./auth";
import { loginResult, signIn } from "./authTestSupport";

beforeEach(() => clearAuth());
afterEach(() => { clearAuth(); vi.unstubAllGlobals(); });

describe("memory-only authentication", () => {
  it("derives a stable scope from authentication, independent of token and login version", () => {
    expect(getAuthScope()).toBeNull();
    signIn("same-agent", "service_agent", "m_demo");
    const first = getAuthScope()!;
    expect(first).toMatchObject({ role: "service_agent", mode: "shop", merchant_id: "m_demo" });
    expect(first.key).not.toContain("test-token");
    signIn("same-agent", "service_agent", "m_demo");
    expect(getAuthScope()!.key).toBe(first.key);
    signIn("same-agent", "service_agent", "m_second");
    expect(getAuthScope()!.key).not.toBe(first.key);
    signIn("same-agent", "buyer");
    expect(getAuthScope()).toMatchObject({ mode: "platform", merchant_id: null });
  });

  it("does not reinterpret an unbound service agent as a platform user", () => {
    signIn("invalid-agent", "service_agent", null);
    expect(getAuthScope()).toBeNull();
  });

  it("keeps authenticated scope independent of later mutation of a response object", () => {
    const attempt = beginLogin();
    const response = loginResult("agent", "service_agent", "m_demo");
    completeLogin(attempt.version, response);
    response.user.merchant_id = "m_second";
    expect(getAuthScope()!.merchant_id).toBe("m_demo");
  });
  it("publishes login state transitions and cancels the old request lifetime", () => {
    const changed = vi.fn();
    const unsubscribe = subscribeAuth(changed);
    expect(getCredentials()).toBeNull();
    const attempt = beginLogin();
    expect(getAuthSnapshot().status).toBe("signing_in");
    expect(completeLogin(attempt.version, loginResult())).toBe(true);
    expect(getAuthSnapshot().user?.user_id).toBe("buyer-a");
    expect(getCredentials()?.token).toBe("test-token-buyer-a");
    expect(JSON.stringify(getAuthSnapshot())).not.toContain("test-token");
    clearAuth();
    expect(attempt.signal.aborted).toBe(true);
    expect(getAuthSnapshot().status).toBe("signed_out");
    expect(getCredentials()).toBeNull();
    expect(changed).toHaveBeenCalledTimes(3);
    unsubscribe();
    beginLogin();
    expect(changed).toHaveBeenCalledTimes(3);
  });

  it("ignores an old login result and old-account unauthorized notification", () => {
    const first = beginLogin();
    const second = beginLogin();
    expect(completeLogin(first.version, loginResult("a"))).toBe(false);
    expect(completeLogin(second.version, loginResult("b"))).toBe(true);
    expect(invalidateAuth(first.version)).toBe(false);
    expect(getAuthSnapshot().user?.user_id).toBe("b");
    expect(invalidateAuth(second.version)).toBe(true);
    expect(getAuthSnapshot().notice).toContain("登录已失效");
  });

  it("never reads or writes browser credential storage", () => {
    const storage = { getItem: vi.fn(() => { throw new Error("unexpected read"); }), setItem: vi.fn(() => { throw new Error("unexpected write"); }) };
    vi.stubGlobal("localStorage", storage);
    vi.stubGlobal("sessionStorage", storage);
    signIn(); clearAuth();
    expect(storage.getItem).not.toHaveBeenCalled();
    expect(storage.setItem).not.toHaveBeenCalled();
  });
});
