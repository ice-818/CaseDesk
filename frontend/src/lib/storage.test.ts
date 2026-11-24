import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearAuth, getAuthScope } from "./auth";
import { MemoryStorage, signIn } from "./authTestSupport";
import { captureStorageScope, loadConversations, loadTheme, saveConversations, saveTheme } from "./storage";
import type { Conversation } from "../types";

const conversation: Conversation = {
  id: "private-a", title: "A的咨询", updatedAt: 1,
  messages: [{ id: "m", role: "assistant", content: "私有内容", state: "streaming", pendingAction: { action_id: "a", tool_name: "refund", arguments: {}, risk: "high", summary: "confirm" } }],
};
let storage: MemoryStorage;
beforeEach(() => { clearAuth(); storage = new MemoryStorage(); vi.stubGlobal("localStorage", storage); });
afterEach(() => { clearAuth(); vi.unstubAllGlobals(); });

describe("account-partitioned cache", () => {
  it("isolates conversations and theme by encoded authenticated user id", () => {
    signIn("buyer:a/中文");
    const a = captureStorageScope();
    saveConversations([conversation], a); saveTheme("dark", a);
    const key = `casedesk-web:v3:${encodeURIComponent("buyer:a/中文")}:${encodeURIComponent(getAuthScope()!.key)}`;
    expect(storage.getItem(key)).toContain("A的咨询");
    expect(storage.getItem(`${key}:theme`)).toBe("dark");
    signIn("buyer-b");
    const b = captureStorageScope();
    expect(loadConversations(b)).toEqual([]);
    expect(loadTheme(b)).toBe("light");
    saveTheme("light", b);
    // A's delayed timer/pagehide handler cannot write into B or even overwrite A.
    saveConversations([], a); saveTheme("light", a);
    expect(loadConversations(a)).toEqual([]);
    signIn("buyer:a/中文");
    const again = captureStorageScope();
    expect(loadConversations(again)[0]?.id).toBe("private-a");
    expect(loadConversations(again)[0]?.messages[0]?.state).toBe("stopped");
    expect(loadConversations(again)[0]?.messages[0]?.pendingAction).toBeNull();
    expect(loadTheme(again)).toBe("dark");
    expect(storage.getItem(key)).not.toContain("refund");
  });

  it("does not claim, migrate, remove or display global legacy caches", () => {
    const originals = {
      "casedesk-web:v1": JSON.stringify([conversation]),
      "adaptive-rag-web:v1": JSON.stringify([conversation]),
      "casedesk-theme": "dark", "adaptive-rag-theme": "dark",
      "casedesk-web:v2:buyer-a": JSON.stringify([conversation]),
      "casedesk-web:v2:buyer-a:theme": "dark",
    };
    for (const [key, value] of Object.entries(originals)) storage.setItem(key, value);
    signIn();
    const scope = captureStorageScope();
    expect(loadConversations(scope)).toEqual([]);
    expect(loadTheme(scope)).toBe("light");
    expect(storage.length).toBe(6);
    for (const [key, value] of Object.entries(originals)) expect(storage.getItem(key)).toBe(value);
    saveConversations([], scope);
    expect(storage.length).toBe(7);
  });

  it("denies anonymous access and tolerates disabled or corrupt storage", () => {
    expect(captureStorageScope).toThrow("登录");
    signIn();
    const scope = captureStorageScope();
    storage.setItem(`casedesk-web:v3:buyer-a:${encodeURIComponent(scope.scopeId)}`, "invalid JSON");
    expect(loadConversations(scope)).toEqual([]);
    vi.stubGlobal("localStorage", { getItem: () => { throw new Error("disabled"); }, setItem: () => { throw new Error("full"); } });
    expect(loadConversations(scope)).toEqual([]);
    expect(loadTheme(scope)).toBe("light");
    expect(() => { saveConversations([conversation], scope); saveTheme("dark", scope); }).not.toThrow();
  });

  it("isolates the same account across merchant bindings and rejects late writes", () => {
    signIn("same-agent", "service_agent", "m_demo");
    const demo = captureStorageScope();
    saveConversations([conversation], demo); saveTheme("dark", demo);
    const original = new Map(Array.from({ length: storage.length }, (_, index) => {
      const key = storage.key(index)!;
      return [key, storage.getItem(key)];
    }));

    signIn("same-agent", "service_agent", "m_second");
    const second = captureStorageScope();
    expect(second.scopeId).not.toBe(demo.scopeId);
    expect(loadConversations(second)).toEqual([]);
    expect(loadTheme(second)).toBe("light");
    saveConversations([{ ...conversation, id: "second-shop" }], second);
    saveTheme("light", second);
    saveConversations([], demo); saveTheme("light", demo);
    // Even a scope whose version was copied to the current login cannot
    // redirect data from the old merchant into the new merchant partition.
    saveConversations([], { ...demo, version: second.version });
    expect(loadConversations(second)[0]?.id).toBe("second-shop");
    for (const [key, value] of original) expect(storage.getItem(key)).toBe(value);

    signIn("same-agent", "service_agent", "m_demo");
    const restored = captureStorageScope();
    expect(restored.scopeId).toBe(demo.scopeId);
    expect(loadConversations(restored)[0]?.id).toBe(conversation.id);
    expect(loadTheme(restored)).toBe("dark");
  });

  it("isolates role changes for the same user and retains the original partition", () => {
    signIn("same-account", "buyer");
    const buyer = captureStorageScope();
    saveConversations([conversation], buyer); saveTheme("dark", buyer);
    for (const role of ["operator", "service_agent"] as const) {
      signIn("same-account", role);
      const changed = captureStorageScope();
      expect(changed.scopeId).not.toBe(buyer.scopeId);
      expect(loadConversations(changed)).toEqual([]);
      expect(loadTheme(changed)).toBe("light");
      saveConversations([], buyer);
    }
    signIn("same-account", "buyer");
    expect(loadConversations(captureStorageScope())[0]?.id).toBe(conversation.id);
    expect(loadTheme(captureStorageScope())).toBe("dark");
  });

  it("never claims an old user-only cache for a shop login", () => {
    const key = "casedesk-web:v2:same-agent";
    storage.setItem(key, JSON.stringify([conversation]));
    storage.setItem(`${key}:theme`, "dark");
    for (const merchant of ["m_demo", "m_second"]) {
      signIn("same-agent", "service_agent", merchant);
      expect(loadConversations(captureStorageScope())).toEqual([]);
      expect(loadTheme(captureStorageScope())).toBe("light");
    }
    expect(storage.length).toBe(2);
    expect(storage.getItem(key)).toBe(JSON.stringify([conversation]));
    expect(storage.getItem(`${key}:theme`)).toBe("dark");
  });
});
