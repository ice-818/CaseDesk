import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { apiRequest, loginRequest, logoutRequest, streamChat } from "./api";
import { clearAuth, getAuthSnapshot, getCredentials } from "./auth";
import { loginResult, signIn } from "./authTestSupport";

const fetchMock = vi.fn<typeof fetch>();
beforeEach(() => { clearAuth(); fetchMock.mockReset(); vi.stubGlobal("fetch", fetchMock); signIn(); });
afterEach(() => { clearAuth(); vi.unstubAllGlobals(); });

function streamResponse(text: string, cancel = vi.fn()) {
  return new Response(new ReadableStream<Uint8Array>({ start(controller) { controller.enqueue(new TextEncoder().encode(text)); }, cancel }), {
    headers: { "Content-Type": "text/event-stream" },
  });
}

describe("authenticated CaseDesk requests", () => {
  it("adds Bearer to JSON requests and does not replay writes", async () => {
    fetchMock.mockResolvedValueOnce(Response.json({ ok: true }));
    await expect(apiRequest("/sessions/s/close", { reason: "resolved" })).resolves.toEqual({ ok: true });
    const [url, options] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/api/v1/sessions/s/close");
    expect(new Headers(options?.headers).get("Authorization")).toBe("Bearer test-token-buyer-a");
    expect(JSON.parse(String(options?.body))).toEqual({ reason: "resolved" });
    fetchMock.mockResolvedValueOnce(new Response("", { status: 503 }));
    await expect(apiRequest("/sessions/s/close", { reason: "resolved" })).rejects.toMatchObject({ status: 503 });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("sends login without old Bearer and uses one generic login error", async () => {
    fetchMock.mockResolvedValueOnce(Response.json(loginResult()));
    await loginRequest("buyer-a", "private-password");
    const [, options] = fetchMock.mock.calls[0]!;
    expect(new Headers(options?.headers).has("Authorization")).toBe(false);
    expect(options?.cache).toBe("no-store");
    expect(JSON.parse(String(options?.body))).toEqual({ user_id: "buyer-a", password: "private-password" });
    for (const status of [401, 404]) {
      fetchMock.mockResolvedValueOnce(Response.json({ message: "account exists / password mismatch" }, { status }));
      await expect(loginRequest("unknown", "wrong")).rejects.toThrow("登录失败，请检查账号和密码或稍后重试。");
    }
  });

  it("invalidates on HTTP 401 even with a non-JSON response", async () => {
    const credentials = getCredentials()!;
    fetchMock.mockResolvedValue(new Response("unauthorized", { status: 401 }));
    await expect(apiRequest("/auth/me")).rejects.toMatchObject({ status: 401 });
    expect(getAuthSnapshot().status).toBe("signed_out");
    expect(credentials.signal.aborted).toBe(true);
  });

  it("keeps identity on 403, 503 and network failure", async () => {
    for (const status of [403, 503]) {
      fetchMock.mockResolvedValueOnce(new Response("", { status }));
      await expect(apiRequest("/admin/platform/datasets")).rejects.toMatchObject({ status });
      expect(getAuthSnapshot().status).toBe("signed_in");
    }
    fetchMock.mockRejectedValueOnce(new TypeError("network unavailable"));
    await expect(apiRequest("/auth/me")).rejects.toThrow("network unavailable");
    expect(getAuthSnapshot().status).toBe("signed_in");
  });

  it("ignores a delayed 401 from the previous account", async () => {
    let reply!: (response: Response) => void;
    fetchMock.mockReturnValueOnce(new Promise(resolve => { reply = resolve; }));
    const pending = apiRequest("/auth/me");
    signIn("buyer-b");
    reply(new Response("", { status: 401 }));
    await expect(pending).rejects.toMatchObject({ status: 401 });
    expect(getAuthSnapshot().user?.user_id).toBe("buyer-b");
  });

  it("discards late successful data after account switching", async () => {
    let reply!: (response: Response) => void;
    fetchMock.mockReturnValueOnce(new Promise(resolve => { reply = resolve; }));
    const pending = apiRequest("/sessions/s/reception");
    const requestSignal = fetchMock.mock.calls[0]![1]!.signal!;
    signIn("buyer-b");
    expect(requestSignal.aborted).toBe(true);
    reply(Response.json({ private: "buyer-a" }));
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
  });

  it("blocks anonymous requests and token query parameters before fetch", async () => {
    await expect(apiRequest("/auth/me?access_token=secret")).rejects.toThrow("URL");
    await expect(apiRequest("https://outside.invalid/api")).rejects.toThrow("无效");
    clearAuth();
    await expect(apiRequest("/auth/me")).rejects.toMatchObject({ status: 401 });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("revoke request survives clearing local identity and accepts 204", async () => {
    let reply!: (response: Response) => void;
    fetchMock.mockReturnValueOnce(new Promise(resolve => { reply = resolve; }));
    const credentials = getCredentials()!;
    const pending = logoutRequest(credentials);
    clearAuth();
    const [, options] = fetchMock.mock.calls[0]!;
    expect(options?.signal?.aborted).toBe(false);
    expect(new Headers(options?.headers).get("Authorization")).toBe(`Bearer ${credentials.token}`);
    reply(new Response(null, { status: 204 }));
    await expect(pending).resolves.toBeUndefined();
  });
});

describe("authenticated SSE", () => {
  it("treats stream 401 as terminal and drops all later answer frames", async () => {
    const cancel = vi.fn();
    fetchMock.mockResolvedValueOnce(streamResponse('data: {"event":"token","text":"你好"}\r\n\r\nevent: error\r\ndata: {"status_code":401,"message":"revoked"}\r\n\r\ndata: {"event":"answer","data":{}}\r\n\r\n', cancel));
    const receive = vi.fn();
    await expect(streamChat({ message: "查订单" }, receive, new AbortController().signal)).rejects.toMatchObject({ status: 401 });
    expect(getAuthSnapshot().status).toBe("signed_out");
    expect(receive.mock.calls).toEqual([[{ event: "token", text: "你好" }]]);
    expect(cancel).toHaveBeenCalledTimes(1);
    expect(new Headers(fetchMock.mock.calls[0]![1]?.headers).get("Authorization")).toBe("Bearer test-token-buyer-a");
    expect(fetchMock.mock.calls[0]![1]?.body).toBe('{"message":"查订单"}');
  });

  it("keeps login on terminal stream 403/503", async () => {
    for (const status of [403, 503]) {
      fetchMock.mockResolvedValueOnce(streamResponse(`data: {"event":"error","status_code":${status}}\n\n`));
      await expect(streamChat({ message: "test" }, vi.fn(), new AbortController().signal)).rejects.toMatchObject({ status });
      expect(getAuthSnapshot().status).toBe("signed_in");
    }
  });

  it("releases a stalled stream when the account is cleared", async () => {
    let started!: () => void;
    const ready = new Promise<void>(resolve => { started = resolve; });
    const cancel = vi.fn();
    const response = new Response(new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode('data: {"event":"token","text":"first"}\n\n')); }, cancel }));
    fetchMock.mockResolvedValueOnce(response);
    const pending = streamChat({ message: "test" }, () => started(), new AbortController().signal);
    const assertion = expect(pending).rejects.toMatchObject({ name: "AbortError" });
    await ready;
    clearAuth();
    await assertion;
    expect(cancel).toHaveBeenCalledTimes(1);
  });
});
