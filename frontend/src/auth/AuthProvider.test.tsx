import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AuthProvider, useAuth } from "./AuthProvider";
import { clearAuth, getAuthSnapshot, getCredentials } from "../lib/auth";
import { loginResult, signIn } from "../lib/authTestSupport";

function actions() {
  let value!: ReturnType<typeof useAuth>;
  function Capture() { value = useAuth(); return null; }
  renderToStaticMarkup(<AuthProvider><Capture /></AuthProvider>);
  return value;
}

const fetchMock = vi.fn<typeof fetch>();
beforeEach(() => { clearAuth(); fetchMock.mockReset(); vi.stubGlobal("fetch", fetchMock); });
afterEach(() => { clearAuth(); vi.unstubAllGlobals(); });

describe("AuthProvider actions", () => {
  it("ignores an older login failure while a new account is already authenticated", async () => {
    let reject!: (error: Error) => void;
    fetchMock.mockReturnValueOnce(new Promise((_resolve, fail) => { reject = fail; }));
    const auth = actions();
    const oldLogin = auth.login("old", "old-password");
    fetchMock.mockResolvedValueOnce(Response.json(loginResult("new")));
    expect(await auth.login("new", "new-password")).toBe(true);
    reject(new Error("old request failed"));
    expect(await oldLogin).toBe(false);
    expect(getAuthSnapshot().user?.user_id).toBe("new");
    expect(getAuthSnapshot().notice).toBe("");
  });

  it("authenticates with the backend and exposes generic failed-login notices", async () => {
    const auth = actions();
    fetchMock.mockResolvedValueOnce(Response.json(loginResult()));
    expect(await auth.login("buyer-a", "private-password")).toBe(true);
    expect(getAuthSnapshot().status).toBe("signed_in");
    expect(getCredentials()?.token).toBe(loginResult().access_token);
    fetchMock.mockResolvedValueOnce(Response.json({ detail: "user not found" }, { status: 401 }));
    expect(await auth.login("unknown", "wrong")).toBe(false);
    expect(getCredentials()).toBeNull();
    expect(getAuthSnapshot().notice).toBe("登录失败，请检查账号和密码或稍后重试。");
    expect(getAuthSnapshot().notice).not.toContain("user not found");
  });

  it("clears the workspace identity before waiting for server logout", async () => {
    const credentials = signIn();
    let reply!: (response: Response) => void;
    fetchMock.mockReturnValueOnce(new Promise(resolve => { reply = resolve; }));
    const pending = actions().logout();
    expect(getAuthSnapshot().status).toBe("signed_out");
    expect(credentials.signal.aborted).toBe(true);
    reply(new Response(null, { status: 204 }));
    await pending;
    expect(getAuthSnapshot().notice).toBe("");
  });

  it("late logout failure cannot affect a newly logged-in account", async () => {
    signIn("old");
    let reject!: (error: Error) => void;
    fetchMock.mockReturnValueOnce(new Promise((_resolve, fail) => { reject = fail; }));
    const pending = actions().logout();
    signIn("new");
    reject(new Error("network"));
    await pending;
    expect(getAuthSnapshot().user?.user_id).toBe("new");
    expect(getAuthSnapshot().notice).toBe("");
  });
});
