"""Conversation session and message persistence."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.product_requirements import ProductRequirements
from app.core.data_scope import require_data_scope
from app.core.error_codes import ErrorCode
from app.core.exceptions import AppError, ForbiddenError, ResourceNotFoundError
from app.core.time import utcnow
from app.repositories.models import (
    MessageRow,
    ReceptionFeedbackRow,
    ReceptionRow,
    SessionRow,
    ShoppingTaskRow,
)
from app.repositories.scope_policy import session_condition
from app.schemas.chat import ChatMessage
from app.schemas.reception import ReceptionFeedbackRequest


class SessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self.scope = require_data_scope()

    async def authorized(self, session_id: str, *, write: bool = False) -> SessionRow:
        row = await self._session.scalar(select(SessionRow).where(SessionRow.session_id == session_id, session_condition(self.scope)).execution_options(populate_existing=True))
        if row is None:
            if write:
                raise ForbiddenError()
            raise ResourceNotFoundError()
        return row

    async def get_or_create(self, session_id: str, user_id: str) -> SessionRow:
        self.scope.assert_current()
        row = await self._session.get(SessionRow, session_id)
        if row is None:
            row = SessionRow(session_id=session_id, user_id=self.scope.actor_user_id,
                             channel="shop" if self.scope.mode == "shop" else "platform",
                             merchant_id=self.scope.merchant_id)
            self._session.add(row)
            await self._session.flush()
        else:
            row = await self.authorized(session_id, write=True)
        return row

    async def reception(
        self,
        session_id: str,
        user_id: str,
        *,
        idle_seconds: int,
        now: datetime | None = None,
    ) -> ReceptionRow:
        session = await self.authorized(session_id)
        row = await self._session.get(ReceptionRow, session_id)
        if row is None:
            # Existing sessions start from their actual last activity, not from
            # this status request; checking status never refreshes the timeout.
            row = ReceptionRow(session_id=session_id, last_activity_at=session.updated_at)
            self._session.add(row)
            await self._session.flush()
        current = now or utcnow()
        last = row.last_activity_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        deadline = last + timedelta(seconds=idle_seconds)
        if row.status == "active" and current >= deadline:
            row.status = "closed"
            row.end_reason = "timeout"
            row.closed_at = deadline
            await self._session.flush()
        return row

    async def ensure_active(self, session_id: str, user_id: str, *, idle_seconds: int) -> None:
        await self.authorized(session_id, write=True)
        row = await self.reception(session_id, user_id, idle_seconds=idle_seconds)
        if row.status != "active":
            # Persist a newly detected timeout even though this chat is rejected.
            await self._session.commit()
            raise AppError(
                "reception closed; start a new session",
                error_code=ErrorCode.INVALID_REQUEST,
                http_status=409,
            )

    async def close_reception(
        self,
        session_id: str,
        user_id: str,
        reason: str,
        *,
        idle_seconds: int,
    ) -> ReceptionRow:
        if reason not in {"resolved", "user_closed"}:
            raise ValueError("invalid user close reason")
        await self.authorized(session_id, write=True)
        row = await self.reception(session_id, user_id, idle_seconds=idle_seconds)
        if row.status == "active":
            row.status = "closed"
            row.end_reason = reason
            row.closed_at = utcnow()
            await self._session.flush()
        return row

    async def shopping_task(self, session_id: str) -> ProductRequirements | None:
        await self.authorized(session_id)
        row = await self._session.get(ShoppingTaskRow, session_id)
        return ProductRequirements.model_validate_json(row.requirements_json) if row else None

    async def save_shopping_task(self, session_id: str, requirements: ProductRequirements) -> None:
        await self.authorized(session_id, write=True)
        row = await self._session.get(ShoppingTaskRow, session_id)
        if row is None:
            self._session.add(
                ShoppingTaskRow(
                    session_id=session_id, requirements_json=requirements.model_dump_json()
                )
            )
        else:
            row.requirements_json = requirements.model_dump_json()

    async def feedback(self, session_id: str) -> ReceptionFeedbackRow | None:
        await self.authorized(session_id)
        return await self._session.get(ReceptionFeedbackRow, session_id)

    async def submit_feedback(
        self, session_id: str, payload: ReceptionFeedbackRequest, *, user_id: str, idle_seconds: int
    ) -> ReceptionFeedbackRow:
        await self.authorized(session_id, write=True)
        reception = await self.reception(session_id, user_id, idle_seconds=idle_seconds)
        if reception.status != "closed":
            raise AppError(
                "close reception before rating",
                error_code=ErrorCode.INVALID_REQUEST,
                http_status=409,
            )
        previous = await self.feedback(session_id)
        if previous is not None:
            if (previous.score, previous.resolved, previous.reason) != (
                payload.score,
                payload.resolved,
                payload.reason,
            ):
                raise AppError(
                    "feedback already submitted",
                    error_code=ErrorCode.INVALID_REQUEST,
                    http_status=409,
                )
            return previous
        last_message = await self._session.scalar(
            select(MessageRow.id)
            .where(MessageRow.session_id == session_id, MessageRow.role == "assistant")
            .order_by(MessageRow.id.desc())
            .limit(1)
        )
        row = ReceptionFeedbackRow(
            session_id=session_id,
            score=payload.score,
            resolved=payload.resolved,
            reason=payload.reason,
            last_assistant_message_id=last_message,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def recent_messages(self, session_id: str, *, limit: int = 20) -> list[ChatMessage]:
        await self.authorized(session_id)
        result = await self._session.execute(
            select(MessageRow)
            .where(MessageRow.session_id == session_id)
            .order_by(MessageRow.id.desc())
            .limit(limit)
        )
        rows = list(reversed(result.scalars().all()))
        return [ChatMessage(role=row.role, content=row.content) for row in rows]  # type: ignore[arg-type]

    async def append_message(self, session_id: str, role: str, content: str) -> None:
        row = await self.authorized(session_id, write=True)
        self._session.add(MessageRow(session_id=session_id, role=role, content=content))
        row.updated_at = utcnow()
        reception = await self._session.get(ReceptionRow, session_id)
        if reception is not None and reception.status == "active":
            reception.last_activity_at = utcnow()

    async def save_summary(self, session_id: str, summary: str) -> None:
        row = await self.authorized(session_id, write=True)
        row.summary = summary
        row.updated_at = utcnow()
