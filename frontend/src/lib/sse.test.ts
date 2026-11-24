import { describe, expect, it } from "vitest";
import { SseDecoder } from "./sse";

describe("SseDecoder", () => {
  it("decodes a fragmented UTF-8 CRLF terminal unauthorized event", () => {
    const decoder = new SseDecoder();
    const bytes = new TextEncoder().encode('event: error\r\ndata: {"status_code":401,"error_code":"UNAUTHORIZED","message":"登录失效","trace_id":"t"}\r\n\r\n');
    const events = [];
    for (const byte of bytes) events.push(...decoder.feed(Uint8Array.of(byte)));
    expect(events).toEqual([{ event: "error", status_code: 401, error_code: "UNAUTHORIZED", message: "登录失效", trace_id: "t" }]);
  });

  it("retains error fields when the event name is also inside JSON", () => {
    const decoder = new SseDecoder();
    decoder.feed(new TextEncoder().encode('event: error\ndata: {"event":"error","status_code":401}'));
    expect(decoder.finish()).toEqual([{ event: "error", status_code: 401 }]);
  });
  it("reassembles split UTF-8 and frame boundaries", () => {
    const decoder = new SseDecoder();
    const bytes = new TextEncoder().encode('data: {"event":"token","text":"你好"}\n\ndata: {"event":"done","trace_id":"t"}\n\n');
    const cut = bytes.indexOf(0xe5) + 1;
    expect(decoder.feed(bytes.slice(0, cut))).toEqual([]);
    expect(decoder.feed(bytes.slice(cut, bytes.length - 1))).toEqual([
      { event: "token", text: "你好" },
    ]);
    expect(decoder.feed(bytes.slice(bytes.length - 1))).toEqual([
      { event: "done", trace_id: "t" },
    ]);
  });

  it("handles CRLF, comments and an unterminated final frame", () => {
    const decoder = new SseDecoder();
    expect(decoder.feed(new TextEncoder().encode(': ping\r\ndata: {"event":"started","trace_id":"x"}\r\n\r\n'))).toEqual([
      { event: "started", trace_id: "x" },
    ]);
    decoder.feed(new TextEncoder().encode('data: {"event":"done","trace_id":"x"}'));
    expect(decoder.finish()).toEqual([{ event: "done", trace_id: "x" }]);
  });
});
