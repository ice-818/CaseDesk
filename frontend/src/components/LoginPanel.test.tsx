import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { AuthProvider } from "../auth/AuthProvider";
import { beginLogin, clearAuth, invalidateAuth } from "../lib/auth";
import { signIn } from "../lib/authTestSupport";
import { LoginPanel } from "./LoginPanel";

beforeEach(() => clearAuth());
afterEach(() => clearAuth());
const render = () => renderToStaticMarkup(<AuthProvider><LoginPanel /></AuthProvider>);

describe("LoginPanel", () => {
  it("renders a labeled masked empty password and disables autocomplete", () => {
    const html = render();
    expect(html).toContain('for="login-user"');
    expect(html).toContain('for="login-password"');
    expect(html).toMatch(/id="login-password"[^>]*type="password"[^>]*autoComplete="off"[^>]*value=""/);
    expect(html).toContain("刷新页面后需重新登录");
    expect(html).not.toContain("u_demo");
    expect(html).toContain('type="submit" disabled=""');
  });

  it("shows signing-in state and locks the form", () => {
    beginLogin();
    const html = render();
    expect(html).toContain("正在登录…");
    expect(html.match(/disabled=""/g)).toHaveLength(3);
  });

  it("displays a safe expiry notice without any old credential", () => {
    const attempt = signIn(); invalidateAuth(attempt.version);
    const html = render();
    expect(html).toContain('role="alert"');
    expect(html).toContain("登录已失效，请重新登录。");
    expect(html).not.toContain("test-token");
    expect(html).not.toContain("buyer-a");
  });
});
