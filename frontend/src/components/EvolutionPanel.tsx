import { useEffect, useRef, useState } from "react";
import { apiRequest } from "../lib/api";
import { useAuth } from "../auth/AuthProvider";
import type {
  EvolutionProposal,
  EvolutionStatus,
  FeedbackCase,
  FeedbackCasesResponse,
  RegressionDataset,
} from "../types";

const INTENT_OPTIONS = [
  "greeting",
  "chitchat",
  "faq_policy",
  "order_inquiry",
  "logistics",
  "refund",
  "return_exchange",
  "cancel_order",
  "product_consult",
  "recommendation",
  "coupon",
  "complaint",
  "human_handoff",
  "out_of_scope",
];

const STATUS_LABEL: Record<EvolutionStatus, string> = {
  collect: "收集错例",
  attribute: "待回归",
  regress: "待门禁",
  gate: "待人审",
  review: "待晋升",
  promoted: "已晋升",
  blocked: "门禁阻断",
  rejected: "已驳回",
};

export function StatusBadge({ status }: { status: EvolutionStatus }) {
  return (
    <span className={`evolution-badge evolution-badge-${status}`}>
      {STATUS_LABEL[status]}
    </span>
  );
}

export function FeedbackCaseCard({ item }: { item: FeedbackCase }) {
  return (
    <article className="feedback-case">
      <header className="feedback-case-head">
        <strong>{item.case_id}</strong>
        <span>评分 {item.score}/5</span>
        <span>{item.channel === "platform" ? "平台渠道" : "店铺渠道"}</span>
      </header>
      {item.feedback_reason && (
        <p className="feedback-reason">反馈原因：{item.feedback_reason}</p>
      )}
      <div className="feedback-turns">
        {item.turns.map((turn, index) => (
          <p key={index} className={`feedback-turn feedback-turn-${turn.role}`}>
            <span className="feedback-role">
              {turn.role === "user" ? "用户" : turn.role === "assistant" ? "客服" : "系统"}
            </span>
            {turn.content}
          </p>
        ))}
      </div>
    </article>
  );
}

export function EvolutionPanel({ onBack }: { onBack: () => void }) {
  const auth = useAuth();
  const requestScope = useRef(new AbortController());
  const [proposals, setProposals] = useState<EvolutionProposal[]>([]);
  const [datasets, setDatasets] = useState<RegressionDataset[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [feedbackCases, setFeedbackCases] = useState<FeedbackCase[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  // Create-proposal form.
  const [newName, setNewName] = useState("");
  const [newProposalVersion, setNewProposalVersion] = useState("v1");
  // Regression form.
  const [regressionId, setRegressionId] = useState("");
  const [candidateIntent, setCandidateIntent] = useState("logistics");
  const [keyword, setKeyword] = useState("");
  // Human review form.
  const [reviewNote, setReviewNote] = useState("");
  // Consolidation form.
  const [consolidateVersion, setConsolidateVersion] = useState("");
  // Regression dataset review form.
  const [datasetNote, setDatasetNote] = useState("");

  const selected = proposals.find((p) => p.proposal_id === selectedId) ?? null;
  const approvedDatasets = datasets.filter((d) => d.status === "approved");

  async function reload() {
    const [proposalData, datasetData] = await Promise.all([
      apiRequest<{ proposals: EvolutionProposal[] }>(
        "/admin/evolution/proposals",
        undefined,
        requestScope.current.signal
      ),
      apiRequest<{ datasets: RegressionDataset[] }>(
        "/admin/evolution/regression-datasets",
        undefined,
        requestScope.current.signal
      ),
    ]);
    setProposals(proposalData.proposals);
    setDatasets(datasetData.datasets);
  }

  async function loadFeedback(id: string) {
    const data = await apiRequest<FeedbackCasesResponse>(
      `/admin/evolution/proposals/${encodeURIComponent(id)}/feedback-cases`,
      undefined,
      requestScope.current.signal
    );
    setFeedbackCases(data.cases);
  }

  async function action(work: () => Promise<void>) {
    if (busy) return;
    const scope = requestScope.current;
    setBusy(true);
    setError("");
    try {
      await work();
    } catch (e) {
      if (!scope.signal.aborted) {
        setError(e instanceof Error ? e.message : "操作失败");
      }
    } finally {
      if (!scope.signal.aborted) setBusy(false);
    }
  }

  useEffect(() => {
    requestScope.current = new AbortController();
    void action(reload);
    return () => requestScope.current.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!selectedId) {
      setFeedbackCases([]);
      return;
    }
    const scope = requestScope.current;
    setFeedbackCases([]);
    loadFeedback(selectedId).catch((e) => {
      if (!scope.signal.aborted) {
        setError(e instanceof Error ? e.message : "加载错例失败");
      }
    });
  }, [selectedId]);

  const base = "/admin/evolution";
  function proposalPath(suffix: string) {
    return `${base}/proposals/${encodeURIComponent(selectedId)}/${suffix}`;
  }

  return (
    <main className="platform-page evolution-page">
      <header>
        <button onClick={onBack}>返回客服工作台</button>
        <h1>CaseDesk 自进化人审工作台</h1>
        <button onClick={() => void auth.logout()}>退出登录</button>
      </header>
      <p>
        机器只做确定性的错例收集、归因、回归与门禁；每个采纳决策都由 operator 人工确认，人审不可绕过。全程离线、外部 API 调用为 0。
      </p>
      <p>当前账号：{auth.user?.user_id}；审核人由服务端登录身份确定。</p>
      <button disabled={busy} onClick={() => void action(reload)}>
        连接／刷新
      </button>
      {busy && <p role="status">正在处理，请等待…</p>}
      {error && <p role="alert">{error}</p>}

      <div className="platform-columns">
        <section>
          <h2>改进提案</h2>
          <label>
            提案名称
            <input
              aria-label="提案名称"
              value={newName}
              disabled={busy}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="例如 feedback_2026w48"
            />
          </label>
          <label>
            提案版本
            <input
              aria-label="提案版本"
              value={newProposalVersion}
              disabled={busy}
              onChange={(e) => setNewProposalVersion(e.target.value)}
            />
          </label>
          <button
            disabled={busy || !newName.trim() || !newProposalVersion.trim()}
            onClick={() =>
              void action(async () => {
                const result = await apiRequest<EvolutionProposal>(
                  `${base}/proposals`,
                  { name: newName.trim(), version: newProposalVersion.trim() },
                  requestScope.current.signal
                );
                setSelectedId(result.proposal_id);
                await reload();
              })
            }
          >
            收集低分反馈并创建提案
          </button>
          <div className="evolution-proposal-list">
            {proposals.length === 0 && <p>暂无提案，可先创建一条。</p>}
            {proposals.map((proposal) => (
              <button
                key={proposal.proposal_id}
                className={`evolution-proposal-item ${
                  proposal.proposal_id === selectedId ? "evolution-proposal-selected" : ""
                }`}
                onClick={() => setSelectedId(proposal.proposal_id)}
              >
                <span className="evolution-proposal-title">{proposal.title}</span>
                <StatusBadge status={proposal.status} />
              </button>
            ))}
          </div>
        </section>

        <section>
          <h2>提案详情与处理</h2>
          {!selected && <p>请在左侧选择一条提案。</p>}
          {selected && (
            <div className="evolution-detail">
              <div className="evolution-detail-head">
                <strong>{selected.title}</strong>
                <StatusBadge status={selected.status} />
              </div>
              <p className="evolution-meta">
                提案编号：{selected.proposal_id}
                <br />
                候选关键词：{selected.candidate_label || "尚未生成"}
                <br />
                更新时间：{selected.updated_at ?? "—"}
              </p>

              <h3>错例对话（{feedbackCases.length}）</h3>
              {feedbackCases.length === 0 && <p>正在加载错例…</p>}
              {feedbackCases.map((item) => (
                <FeedbackCaseCard key={item.case_id} item={item} />
              ))}

              {selected.attribution && (
                <div className="evolution-block">
                  <h3>确定性归因</h3>
                  <p>
                    类别计数：
                    {Object.entries(selected.attribution.counts)
                      .map(([label, count]) => `${label} ${count}`)
                      .join("；")}
                  </p>
                  <ul>
                    {selected.attribution.items.map((item) => (
                      <li key={item.case_id}>
                        {item.case_id}：{item.attribution}（信号：{item.matched_signal}）
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {selected.status === "collect" && (
                <div className="evolution-block">
                  <h3>第一步：执行词法归因</h3>
                  <button
                    disabled={busy}
                    onClick={() =>
                      void action(async () => {
                        await apiRequest(
                          proposalPath("attribute"),
                          {},
                          requestScope.current.signal
                        );
                        await reload();
                      })
                    }
                  >
                    执行归因
                  </button>
                </div>
              )}

              {selected.status === "attribute" && (
                <div className="evolution-block">
                  <h3>第二步：在冻结回归集上运行基线与候选</h3>
                  <label>
                    选择已批准回归集
                    <select
                      aria-label="选择回归集"
                      value={regressionId}
                      onChange={(e) => setRegressionId(e.target.value)}
                    >
                      <option value="">请选择</option>
                      {approvedDatasets.map((d) => (
                        <option key={d.dataset_id} value={d.dataset_id}>
                          {d.name}/{d.version} · {d.payload.cases.length} 条
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    候选意图
                    <select
                      aria-label="候选意图"
                      value={candidateIntent}
                      onChange={(e) => setCandidateIntent(e.target.value)}
                    >
                      {INTENT_OPTIONS.map((value) => (
                        <option key={value} value={value}>
                          {value}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    候选关键词
                    <input
                      aria-label="候选关键词"
                      value={keyword}
                      onChange={(e) => setKeyword(e.target.value)}
                      placeholder="例如 催发货"
                    />
                  </label>
                  <button
                    disabled={busy || !regressionId || !keyword.trim()}
                    onClick={() =>
                      void action(async () => {
                        await apiRequest(
                          proposalPath("regress"),
                          {
                            regression_dataset_id: regressionId,
                            candidate: {
                              type: "intent_keyword",
                              intent: candidateIntent,
                              keyword: keyword.trim(),
                            },
                          },
                          requestScope.current.signal
                        );
                        await reload();
                      })
                    }
                  >
                    运行基线与候选回归
                  </button>
                </div>
              )}

              {selected.status === "regress" && (
                <div className="evolution-block">
                  <h3>第三步：执行 fail-closed 门禁</h3>
                  <button
                    disabled={busy}
                    onClick={() =>
                      void action(async () => {
                        await apiRequest(
                          proposalPath("gate"),
                          {},
                          requestScope.current.signal
                        );
                        await reload();
                      })
                    }
                  >
                    执行门禁判定
                  </button>
                </div>
              )}

              {selected.gate && (
                <div className="evolution-block">
                  <h3>门禁结果</h3>
                  <p>
                    基线通过率 {selected.gate.baseline_pass_rate.toFixed(2)} → 候选{" "}
                    {selected.gate.candidate_pass_rate.toFixed(2)}（Δ{" "}
                    {selected.gate.pass_rate_delta.toFixed(2)}）
                  </p>
                  <p>改善用例：{selected.gate.improved.join("、") || "无"}</p>
                  <p>退化用例：{selected.gate.regressed.join("、") || "无"}</p>
                  {selected.gate.reasons.length > 0 && (
                    <ul>
                      {selected.gate.reasons.map((reason) => (
                        <li key={reason}>{reason}</li>
                      ))}
                    </ul>
                  )}
                </div>
              )}

              {selected.status === "gate" && (
                <div className="evolution-block">
                  <h3>第四步：人工审核（强制）</h3>
                  <label>
                    审核说明
                    <textarea
                      aria-label="提案审核说明"
                      rows={3}
                      value={reviewNote}
                      onChange={(e) => setReviewNote(e.target.value)}
                    />
                  </label>
                  {(["approved", "rejected"] as const).map((decision) => (
                    <button
                      key={decision}
                      disabled={busy || !reviewNote.trim()}
                      onClick={() =>
                        void action(async () => {
                          await apiRequest(
                            proposalPath("review"),
                            { decision, note: reviewNote.trim() },
                            requestScope.current.signal
                          );
                          setReviewNote("");
                          await reload();
                        })
                      }
                    >
                      {decision === "approved" ? "审核通过" : "驳回"}
                    </button>
                  ))}
                </div>
              )}

              {selected.status === "review" && (
                <div className="evolution-block">
                  <h3>第五步：确认晋升</h3>
                  <p>人审决策：{selected.review_decision}；审核人：{selected.reviewer}</p>
                  <button
                    disabled={busy}
                    onClick={() =>
                      void action(async () => {
                        await apiRequest(
                          proposalPath("promote"),
                          {},
                          requestScope.current.signal
                        );
                        await reload();
                      })
                    }
                  >
                    确认晋升
                  </button>
                </div>
              )}

              {selected.status === "promoted" && (
                <div className="evolution-block">
                  <h3>第六步：把错例沉淀为新回归版本</h3>
                  <label>
                    新回归版本号
                    <input
                      aria-label="新回归版本号"
                      value={consolidateVersion}
                      onChange={(e) => setConsolidateVersion(e.target.value)}
                      placeholder="例如 v2"
                    />
                  </label>
                  <button
                    disabled={busy || !consolidateVersion.trim()}
                    onClick={() =>
                      void action(async () => {
                        await apiRequest(
                          proposalPath("consolidate"),
                          { new_version: consolidateVersion.trim() },
                          requestScope.current.signal
                        );
                        setConsolidateVersion("");
                        await reload();
                      })
                    }
                  >
                    执行错例沉淀
                  </button>
                </div>
              )}

              {selected.status === "blocked" && (
                <p className="evolution-terminal">门禁硬条件未通过，提案已终止，不可推进。</p>
              )}
              {selected.status === "rejected" && (
                <p className="evolution-terminal">人审已驳回，提案终止。</p>
              )}
            </div>
          )}
        </section>
      </div>

      <section>
        <h2>意图回归集版本管理</h2>
        <p>已批准集不可修改；沉淀或修订都产生新的草稿版本，审核通过后才成为新冻结集。</p>
        {datasets.length === 0 && <p>暂无意图回归集。</p>}
        <div className="evolution-dataset-list">
          {datasets.map((dataset) => (
            <article key={dataset.dataset_id} className="evolution-dataset">
              <header className="feedback-case-head">
                <strong>
                  {dataset.name}/{dataset.version}
                </strong>
                <span className={`evolution-badge evolution-dataset-${dataset.status}`}>
                  {dataset.status === "approved"
                    ? "已冻结"
                    : dataset.status === "draft"
                      ? "待审核"
                      : dataset.status}
                </span>
                <span>{dataset.payload.cases.length} 条用例</span>
              </header>
              {dataset.status === "draft" && (
                <div className="evolution-block">
                  <label>
                    版本审核说明
                    <textarea
                      aria-label={`回归集审核说明 ${dataset.dataset_id}`}
                      rows={2}
                      value={datasetNote}
                      onChange={(e) => setDatasetNote(e.target.value)}
                    />
                  </label>
                  {(["approved", "rejected"] as const).map((decision) => (
                    <button
                      key={decision}
                      disabled={busy || !datasetNote.trim()}
                      onClick={() =>
                        void action(async () => {
                          await apiRequest(
                            `${base}/regression-datasets/${encodeURIComponent(
                              dataset.dataset_id
                            )}/review`,
                            { decision, note: datasetNote.trim() },
                            requestScope.current.signal
                          );
                          setDatasetNote("");
                          await reload();
                        })
                      }
                    >
                      {decision === "approved" ? "冻结该版本" : "驳回草稿"}
                    </button>
                  ))}
                </div>
              )}
              {dataset.reviewer && (
                <small>
                  审核人：{dataset.reviewer}；说明：{dataset.review_note}
                </small>
              )}
            </article>
          ))}
        </div>
      </section>
    </main>
  );
}
