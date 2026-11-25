"""Semi-automatic, human-in-the-loop self-evolution services.

The machine performs only deterministic work: collect low-score feedback,
reconstruct the conversation, and attribute each failure with lexical rules.
No prompt is auto-generated and no change is auto-promoted; the final adoption
decision always stays with a human reviewer.
"""

from sqlalchemy import select

from app.agents.nodes.intent import rule_based_intent
from app.core.exceptions import AppError
from app.core.time import utcnow
from app.repositories.models import (
    MessageRow,
    ReceptionFeedbackRow,
    SessionRow,
)
from app.schemas.evolution import (
    AttributionReport,
    AttributedCase,
    CandidateChange,
    ConversationTurn,
    FeedbackCase,
    FeedbackCaseBundle,
    IntentRegressionBundle,
)

# Ordered by specificity: the first matching signal wins.
_ATTRIBUTION_RULES: list[tuple[str, tuple[str, ...]]] = [
    (
        "tool_permission_error",
        ("权限", "无权", "操作失败", "报错", "不能操作", "无法操作", "403"),
    ),
    (
        "faithfulness_gap",
        ("胡说", "编造", "瞎编", "乱说", "与事实不符", "幻觉", "瞎扯", "不存在"),
    ),
    (
        "retrieval_miss",
        ("找不到", "搜不到", "没查到", "查不到", "没有相关", "无相关", "没有答案"),
    ),
    (
        "intent_error",
        ("答非所问", "理解错", "没听懂", "听不懂", "问东答西", "转人工", "人工"),
    ),
]


class FeedbackCollector:
    """Collect unresolved low-score feedback and reconstruct each case."""

    def __init__(self, factory, settings) -> None:
        self.factory = factory
        self.settings = settings

    async def collect(self, *, name: str, version: str) -> FeedbackCaseBundle:
        async with self.factory() as db:
            feedbacks = list(
                (
                    await db.scalars(
                        select(ReceptionFeedbackRow)
                        .where(
                            ReceptionFeedbackRow.score
                            <= self.settings.feedback_collect_max_score,
                            ReceptionFeedbackRow.resolved.is_(False),
                        )
                        .order_by(ReceptionFeedbackRow.created_at)
                    )
                )
            )
            cases: list[FeedbackCase] = []
            for index, feedback in enumerate(feedbacks, start=1):
                session = await db.get(SessionRow, feedback.session_id)
                if session is None:
                    continue
                messages = list(
                    await db.scalars(
                        select(MessageRow)
                        .where(MessageRow.session_id == feedback.session_id)
                        .order_by(MessageRow.id)
                    )
                )
                turns = [
                    ConversationTurn(role=row.role, content=row.content)
                    for row in messages
                ]
                if not turns:
                    continue
                cases.append(
                    FeedbackCase(
                        case_id="fbc_%04d" % index,
                        session_id=feedback.session_id,
                        user_id=session.user_id,
                        merchant_id=session.merchant_id,
                        channel=session.channel,
                        score=feedback.score,
                        feedback_reason=feedback.reason,
                        turns=turns,
                    )
                )
            if not cases:
                raise AppError(
                    "no eligible low-score feedback to collect", http_status=409
                )
            return FeedbackCaseBundle(
                name=name,
                version=version,
                generated_at=utcnow(),
                cases=cases,
            )


class AttributionEngine:
    """Deterministic lexical failure attribution, no LLM calls."""

    def attribute(self, bundle: FeedbackCaseBundle) -> AttributionReport:
        items: list[AttributedCase] = []
        for case in bundle.cases:
            text = self._case_text(case)
            label, signal = self._match(text)
            items.append(
                AttributedCase(
                    case_id=case.case_id,
                    attribution=label,
                    matched_signal=signal,
                )
            )
        counts: dict[str, int] = {}
        for item in items:
            counts[item.attribution] = counts.get(item.attribution, 0) + 1
        return AttributionReport(items=items, counts=counts)

    @staticmethod
    def _case_text(case: FeedbackCase) -> str:
        parts = [case.feedback_reason]
        parts.extend(turn.content for turn in case.turns if turn.role == "user")
        return " ".join(parts).lower()

    @staticmethod
    def _match(text: str) -> tuple[str, str]:
        for label, signals in _ATTRIBUTION_RULES:
            for signal in signals:
                if signal.lower() in text:
                    return label, signal
        return "answer_quality", "default_unspecified_dissatisfaction"


def evaluate_intent_regression(
    bundle: IntentRegressionBundle, *, candidate: CandidateChange | None = None
) -> dict:
    """Run deterministic intent rules over a frozen regression bundle.

    The baseline run uses shipped rules; the candidate run prepends one
    proposed keyword rule. No LLM or network call is involved.
    """
    extra_rules = None
    if candidate is not None:
        extra_rules = [((candidate.keyword,), candidate.intent)]
    rows: list[dict] = []
    for case in bundle.cases:
        result = rule_based_intent(case.query, extra_rules=extra_rules)
        rows.append(
            {
                "case_id": case.case_id,
                "passed": result.label == case.expected_intent,
                "predicted": result.label.value,
                "expected": case.expected_intent.value,
            }
        )
    passed = sum(row["passed"] for row in rows)
    return {
        "summary": {
            "total": len(rows),
            "passed": passed,
            "failed": len(rows) - passed,
            "pass_rate": passed / len(rows),
        },
        "cases": rows,
    }
