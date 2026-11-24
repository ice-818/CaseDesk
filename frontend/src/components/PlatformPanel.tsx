import { useEffect, useRef, useState } from "react";
import { apiRequest } from "../lib/api";
import { useAuth } from "../auth/AuthProvider";

interface Dataset {
  dataset_id: string; name: string; version: string; task: string; split: string;
  status: string; content_hash: string; sample_count: number; source: string;
}
interface Run {
  run_id: string; dataset_id: string; candidate_label: string; status: string;
  manifest: { code_hash: string; mode: string };
  report: { summary?: { total: number; passed: number; failed: number; errors: number; pass_rate: number; p95_ms: number }; cases?: object[]; error?: string };
}

const EXAMPLE = {
  name: "shopping_updates", version: "v1", task: "shopping_update", split: "regression",
  source_type: "synthetic", source: "人工编写的示例；不是线上用户数据", license: "project-authored",
  samples: [{ case_id: "add-white", group_id: "shoe-color-update",
    input: { turns: ["推荐300元以内黑色42码鞋", "白色也可以"] },
    expected: { constraints: { max_price_cents: 30000, colors: ["白色", "黑色"], size: "42" },
      task_query: "鞋", active: true, clarification_required: false, evidence_tags: [] } }],
};

export function PlatformPanel({ onBack }: { onBack: () => void }) {
  const auth = useAuth();
  const requestScope = useRef(new AbortController());
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [draft, setDraft] = useState(JSON.stringify(EXAMPLE, null, 2));
  const [selected, setSelected] = useState("");
  const [note, setNote] = useState("");
  const [label, setLabel] = useState("current-code");
  const [baseline, setBaseline] = useState("");
  const [candidate, setCandidate] = useState("");
  const [details, setDetails] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function reload() {
    const [data, evaluations] = await Promise.all([
      apiRequest<{ datasets: Dataset[] }>("/admin/platform/datasets", undefined, requestScope.current.signal),
      apiRequest<{ runs: Run[] }>("/admin/platform/runs", undefined, requestScope.current.signal),
    ]);
    setDatasets(data.datasets); setRuns(evaluations.runs);
  }
  async function action(work: () => Promise<void>) {
    if (busy) return;
    const scope = requestScope.current;
    setBusy(true); setError("");
    try { await work(); } catch (e) {
      if (!scope.signal.aborted) setError(e instanceof Error ? e.message : "操作失败");
    } finally { if (!scope.signal.aborted) setBusy(false); }
  }
  useEffect(() => {
    requestScope.current = new AbortController();
    void action(reload);
    return () => requestScope.current.abort();
  }, []);

  const current = datasets.find(item => item.dataset_id === selected);
  return <main className="platform-page">
    <header><button onClick={onBack}>返回客服工作台</button><h1>CaseDesk 数据与评测中台</h1><button onClick={() => void auth.logout()}>退出登录</button></header>
    <p>管理冻结的数据集版本与离线评测。当前支持商品过滤、多轮条件更新；不调用模型API。</p>
    <p>当前账号：{auth.user?.user_id}；审核人由服务端登录身份确定。</p>
    <button disabled={busy} onClick={() => void action(reload)}>连接／刷新</button>
    {busy && <p role="status">正在处理，请等待…</p>}
    {error && <p role="alert">{error}</p>}
    <div className="platform-columns">
      <section><h2>数据中台</h2>
        <p>上传JSON版本，校验结构与重复样本，人工审核后发布。修订内容请创建新版本。</p>
        <label>数据集JSON<textarea aria-label="数据集JSON" rows={18} value={draft} disabled={busy} onChange={e => setDraft(e.target.value)} /></label>
        <button disabled={busy} onClick={() => void action(async () => {
          const result = await apiRequest<Dataset>("/admin/platform/datasets", JSON.parse(draft), requestScope.current.signal);
          setSelected(result.dataset_id); setDetails(result); await reload();
        })}>校验并创建草稿版本</button>
        <label>数据集版本<select value={selected} onChange={e => setSelected(e.target.value)}><option value="">请选择</option>{datasets.map(d => <option key={d.dataset_id} value={d.dataset_id}>{d.name}/{d.version} · {d.status} · {d.sample_count}条</option>)}</select></label>
        {current && <p>任务：{current.task}；划分：{current.split}；来源：{current.source}<br />内容哈希：<code>{current.content_hash}</code></p>}
        <button disabled={!selected || busy} onClick={() => void action(async () => setDetails(await apiRequest(`/admin/platform/datasets/${encodeURIComponent(selected)}`, undefined, requestScope.current.signal)))}>查看版本内容</button>
        <label>审核说明<input value={note} onChange={e => setNote(e.target.value)} /></label>
        {(["approved", "rejected"] as const).map(decision => <button key={decision} disabled={busy || current?.status !== "draft" || !note.trim()} onClick={() => void action(async () => {
          setDetails(await apiRequest(`/admin/platform/datasets/${encodeURIComponent(selected)}/review`, { decision, note }, requestScope.current.signal)); await reload();
        })}>{decision === "approved" ? "审核通过" : "驳回草稿"}</button>)}
      </section>
      <section><h2>评测中台</h2>
        <p>使用左侧审核通过的数据集。标签用于识别运行，实际评测的是当前服务加载的代码；不能仅改标签就切换模型或旧代码。</p>
        <label>本次运行标签<input value={label} onChange={e => setLabel(e.target.value)} /></label>
        <button disabled={busy || current?.status !== "approved" || !label.trim()} onClick={() => void action(async () => {
          const result = await apiRequest<Run>("/admin/platform/runs", { dataset_id: selected, candidate_label: label }, requestScope.current.signal);
          setDetails(result); setCandidate(result.run_id); await reload();
        })}>运行离线评测</button>
        <div className="platform-run-list">{runs.map(run => <article key={run.run_id}>
          <strong>{run.candidate_label}</strong> · {run.status}
          <p>{run.report.summary ? `${run.report.summary.passed}/${run.report.summary.total}通过；错误${run.report.summary.errors}；P95 ${run.report.summary.p95_ms.toFixed(2)}ms` : "尚无完整结果"}</p>
          <small>运行：{run.run_id}<br />代码：{run.manifest.code_hash.slice(0, 12)}</small><br />
          <button disabled={busy} onClick={() => void action(async () => setDetails(await apiRequest(`/admin/platform/runs/${encodeURIComponent(run.run_id)}`, undefined, requestScope.current.signal)))}>查看逐题结果</button>
        </article>)}</div>
        <label>基线运行<select value={baseline} onChange={e => setBaseline(e.target.value)}><option value="">请选择</option>{runs.filter(r => r.status === "completed").map(r => <option key={r.run_id} value={r.run_id}>{r.candidate_label} · {r.run_id}</option>)}</select></label>
        <label>候选运行<select value={candidate} onChange={e => setCandidate(e.target.value)}><option value="">请选择</option>{runs.filter(r => r.status === "completed").map(r => <option key={r.run_id} value={r.run_id}>{r.candidate_label} · {r.run_id}</option>)}</select></label>
        <button disabled={busy || !baseline || !candidate} onClick={() => void action(async () => setDetails(await apiRequest(`/admin/platform/compare?baseline=${encodeURIComponent(baseline)}&candidate=${encodeURIComponent(candidate)}`, undefined, requestScope.current.signal)))}>比较改善与退化</button>
      </section>
    </div>
    {details !== null && <section><h2>结果详情</h2><pre>{JSON.stringify(details, null, 2)}</pre></section>}
  </main>;
}
