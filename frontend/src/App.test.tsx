import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { AuthProvider } from "./auth/AuthProvider";
import { clearAuth } from "./lib/auth";
import { MemoryStorage, signIn } from "./lib/authTestSupport";
import { captureStorageScope, saveConversations } from "./lib/storage";

beforeEach(() => { clearAuth(); vi.stubGlobal("localStorage", new MemoryStorage()); });
afterEach(() => { clearAuth(); vi.unstubAllGlobals(); });
const render = () => renderToStaticMarkup(<AuthProvider><App /></AuthProvider>);

describe("workspace access", () => {
  it("shows only login when signed out", () => {
    const html = render();
    expect(html).toContain("登录 CaseDesk");
    expect(html).not.toContain("数据与评测");
    expect(html).not.toContain("新建咨询");
  });

  it("exposes the management entry only for operators", () => {
    for (const role of ["buyer", "service_agent"] as const) {
      signIn("account", role);
      expect(render()).not.toContain(">数据与评测</button>");
      expect(render()).toContain("退出登录");
    }
    signIn("operator", "operator");
    expect(render()).toContain(">数据与评测</button>");
  });

  it("never displays another account's conversation or previous workspace after logout", () => {
    signIn("buyer-a");
    saveConversations([{ id: "a", title: "A专属退款咨询", updatedAt: 1, messages: [] }], captureStorageScope());
    expect(render()).toContain("A专属退款咨询");
    signIn("buyer-b");
    expect(render()).not.toContain("A专属退款咨询");
    clearAuth();
    const html = render();
    expect(html).toContain("登录 CaseDesk");
    expect(html).not.toContain("A专属退款咨询");
  });
});
