"""Typed models for the human-in-the-loop self-evolution feedback loop."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import IntentLabel


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConversationTurn(StrictModel):
    role: Literal["user", "assistant", "system"]
    content: str = Field(min_length=1, max_length=8000)


class FeedbackCase(StrictModel):
    """A single low-score reception reconstructed from feedback and messages."""

    case_id: str = Field(min_length=1, max_length=64)
    session_id: str = Field(min_length=1, max_length=64)
    user_id: str = Field(min_length=1, max_length=64)
    merchant_id: str | None = Field(default=None, max_length=64)
    channel: Literal["platform", "shop"]
    score: int = Field(ge=1, le=5)
    feedback_reason: str = Field(default="", max_length=1000)
    turns: list[ConversationTurn] = Field(min_length=1, max_length=100)


class FeedbackCaseBundle(StrictModel):
    """Immutable bundle of feedback cases collected in one proposal run."""

    name: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    version: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$")
    generated_at: datetime
    cases: list[FeedbackCase] = Field(min_length=1, max_length=200)


AttributionLabel = Literal[
    "intent_error",
    "retrieval_miss",
    "faithfulness_gap",
    "tool_permission_error",
    "answer_quality",
]


class AttributedCase(StrictModel):
    case_id: str = Field(min_length=1, max_length=64)
    attribution: AttributionLabel
    matched_signal: str = Field(min_length=1, max_length=120)


class AttributionReport(StrictModel):
    items: list[AttributedCase] = Field(min_length=1)
    counts: dict[str, int]


class IntentRegressionCase(StrictModel):
    case_id: str = Field(min_length=1, max_length=64)
    query: str = Field(min_length=1, max_length=500)
    expected_intent: IntentLabel


class IntentRegressionBundle(StrictModel):
    """Frozen (query, expected intent) fixtures used for regression gating."""

    name: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    version: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$")
    cases: list[IntentRegressionCase] = Field(min_length=1, max_length=200)


class CandidateChange(StrictModel):
    """A deterministic, human-reviewable change; never an LLM-generated prompt."""

    type: Literal["intent_keyword"]
    intent: IntentLabel
    keyword: str = Field(min_length=1, max_length=40)


class GateResult(StrictModel):
    passed: bool
    baseline_pass_rate: float
    candidate_pass_rate: float
    pass_rate_delta: float
    improved: list[str]
    regressed: list[str]
    reasons: list[str]


class CreateProposalRequest(StrictModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    version: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$")


class RegressionRunRequest(StrictModel):
    regression_dataset_id: str = Field(min_length=1, max_length=64)
    candidate: CandidateChange


class ProposalReviewRequest(StrictModel):
    decision: Literal["approved", "rejected"]
    note: str = Field(min_length=1, max_length=2000)


class DatasetReviewRequest(StrictModel):
    decision: Literal["approved", "rejected"]
    note: str = Field(min_length=1, max_length=2000)


class ConsolidateRequest(StrictModel):
    new_version: str = Field(
        min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$"
    )
