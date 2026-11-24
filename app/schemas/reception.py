from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class CloseReceptionRequest(BaseModel):
    user_id: Any = Field(default=None, json_schema_extra={"deprecated": True})
    reason: Literal["resolved", "user_closed"]


class ReceptionFeedbackRequest(BaseModel):
    user_id: Any = Field(default=None, json_schema_extra={"deprecated": True})
    score: int = Field(ge=1, le=5, strict=True)
    resolved: bool = Field(strict=True)
    reason: str = Field(default="", max_length=1000)


class ReceptionFeedbackResponse(BaseModel):
    score: int
    resolved: bool
    reason: str
    last_assistant_message_id: int | None


class ReceptionResponse(BaseModel):
    session_id: str
    status: Literal["active", "closed"]
    end_reason: Literal["resolved", "user_closed", "timeout"] | None
    resolved: bool
    last_activity_at: datetime
    closed_at: datetime | None
    feedback: ReceptionFeedbackResponse | None = None

    @field_validator("last_activity_at", "closed_at")
    @classmethod
    def normalize_utc(cls, value: datetime | None) -> datetime | None:
        # SQLite drops timezone metadata; API timestamps must remain stable.
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
