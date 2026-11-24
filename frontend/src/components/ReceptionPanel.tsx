import { useEffect, useState } from "react";
import { apiRequest } from "../lib/api";
import type { Reception, ReceptionFeedback } from "../types";

export function ReceptionPanel({ sessionId, busy, onClosed, onNew }: {
  sessionId: string; busy: boolean; onClosed: (closed: boolean) => void; onNew: () => void;
}) {
  const [reception, setReception] = useState<Reception | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [score, setScore] = useState(0);
  const [resolved, setResolved] = useState("");
  const [reason, setReason] = useState("");
  const path = `/sessions/${encodeURIComponent(sessionId)}`;

  useEffect(() => {
    if (busy || pending) return;
    const controller = new AbortController();
    async function refresh() {
      try {
        const result = await apiRequest<Reception>(`${path}/reception`, undefined, controller.signal);
        if (controller.signal.aborted) return;
        setReception(result);
        onClosed(result.status === "closed");
        setError("");
      } catch (e) {
        if (!controller.signal.aborted) setError(e instanceof Error ? e.message : "暂时无法读取接待状态，请稍后刷新。");
      }
    }
    void refresh();
    const timer = window.setInterval(() => void refresh(), 30000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [path, busy, pending]); // onClosed only reports state for this keyed session.

  async function close(endReason: "resolved" | "user_closed") {
    if (pending || busy) return;
    setPending(true); setError("");
    try {
      const result = await apiRequest<Reception>(`${path}/close`, { reason: endReason });
      setReception(result); onClosed(result.status === "closed");
    } catch (e) { setError(e instanceof Error ? e.message : "结束咨询失败"); }
    finally { setPending(false); }
  }

  async function submit() {
    if (!score || !resolved || pending) return;
    setPending(true); setError("");
    try {
      const feedback = await apiRequest<ReceptionFeedback>(`${path}/feedback`, {
        score, resolved: resolved === "yes", reason,
      });
      setReception(current => current ? { ...current, feedback } : current);
    } catch (e) { setError(e instanceof Error ? e.message : "评价提交失败"); }
    finally { setPending(false); }
  }

  return <section className="reception-panel" aria-label="本次咨询">
    {error && <p role="alert">{error}</p>}
    {reception?.status === "active" && <div className="reception-actions">
      <span>本次咨询进行中</span>
      <button disabled={busy || pending} onClick={() => void close("resolved")}>已解决</button>
      <button disabled={busy || pending} onClick={() => void close("user_closed")}>结束咨询</button>
    </div>}
    {reception?.status === "closed" && <>
      <div className="reception-actions"><strong>{reception.end_reason === "timeout" ? "咨询因超时关闭，未标记为已解决" : reception.end_reason === "resolved" ? "本次咨询已标记解决" : "本次咨询已结束，未标记为已解决"}</strong><button onClick={onNew}>新建咨询</button></div>
      {reception.feedback ? <p role="status">评价已保存：{reception.feedback.score}分；{reception.feedback.resolved ? "用户反馈已解决" : "用户反馈未解决"}。谢谢你的反馈。</p>
        : <form onSubmit={e => { e.preventDefault(); void submit(); }}>
          <p>请评价本次服务（可选）。满意度与问题是否解决分别记录。</p>
          <label>满意度 <select required value={score} disabled={pending} onChange={e => setScore(Number(e.target.value))}><option value={0}>请选择</option>{[1, 2, 3, 4, 5].map(n => <option key={n} value={n}>{n}分</option>)}</select></label>
          <label>问题是否解决 <select required value={resolved} disabled={pending} onChange={e => setResolved(e.target.value)}><option value="">请选择</option><option value="yes">已解决</option><option value="no">未解决</option></select></label>
          <label>原因（可选）<textarea value={reason} maxLength={1000} disabled={pending} onChange={e => setReason(e.target.value)} placeholder="例如答非所问、没有找到商品、处理时间较长" /></label>
          <button type="submit" disabled={!score || !resolved || pending}>{pending ? "正在保存…" : "提交评价"}</button>
        </form>}
    </>}
  </section>;
}
