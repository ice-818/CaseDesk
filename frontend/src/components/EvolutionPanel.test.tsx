import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { AuthProvider } from "../auth/AuthProvider";
import { clearAuth } from "../lib/auth";
import { signIn } from "../lib/authTestSupport";
import { EvolutionPanel, FeedbackCaseCard, StatusBadge } from "./EvolutionPanel";
import type { FeedbackCase } from "../types";

beforeEach(() => {
  clearAuth();
  signIn("operator", "operator");
});
afterEach(() => clearAuth());

const feedbackCase: FeedbackCase = {
  case_id: "case_1",
  session_id: "sess_1",
  user_id: "owner",
  merchant_id: null,
  channel: "platform",
  score: 2,
  feedback_reason: "答非所问",
  turns: [
    { role: "user", content: "催发货" },
    { role: "assistant", content: "请问还有什么可以帮您" },
  ],
};

describe("EvolutionPanel", () => {
  it("renders the workbench shell with empty-state guidance", () => {
    const html = renderToStaticMarkup(
      <AuthProvider>
        <EvolutionPanel onBack={() => {}} />
      </AuthProvider>
    );
    expect(html).toContain("CaseDesk 自进化人审工作台");
    expect(html).toContain("返回客服工作台");
    expect(html).toContain("暂无提案，可先创建一条。");
    expect(html).toContain("暂无意图回归集。");
    expect(html).toContain("收集低分反馈并创建提案");
  });

  it("renders localized labels for every proposal status", () => {
    for (const [status, label] of [
      ["collect", "收集错例"],
      ["attribute", "待回归"],
      ["regress", "待门禁"],
      ["gate", "待人审"],
      ["review", "待晋升"],
      ["promoted", "已晋升"],
      ["blocked", "门禁阻断"],
      ["rejected", "已驳回"],
    ] as const) {
      const html = renderToStaticMarkup(<StatusBadge status={status} />);
      expect(html).toContain(label);
      expect(html).toContain(`evolution-badge-${status}`);
    }
  });

  it("renders feedback score, reason and the reconstructed conversation", () => {
    const html = renderToStaticMarkup(<FeedbackCaseCard item={feedbackCase} />);
    expect(html).toContain("case_1");
    expect(html).toContain("评分 2/5");
    expect(html).toContain("反馈原因：答非所问");
    expect(html).toContain("催发货");
    expect(html).toContain("请问还有什么可以帮您");
    expect(html).toContain("feedback-turn-user");
    expect(html).toContain("feedback-turn-assistant");
  });
});
