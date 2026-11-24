"""Chat routes: single-shot JSON and SSE streaming."""

import json
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sse_starlette import JSONServerSentEvent
from sse_starlette.sse import EventSourceResponse

from app.api.auth_route import AuthenticatedRoute
from app.api.dependencies import get_container, get_current_user
from app.core.error_codes import ErrorCode
from app.core.exceptions import AppError
from app.repositories.session_repository import SessionRepository
from app.repositories.shopping_checkpoint import ShoppingCheckpointRepository
from app.schemas.auth import AuthIdentity
from app.schemas.chat import ChatRequest, ChatResponse
from app.schemas.reception import (
    CloseReceptionRequest,
    ReceptionFeedbackRequest,
    ReceptionFeedbackResponse,
    ReceptionResponse,
)
from app.services.container import AppContainer

router = APIRouter(tags=["chat"], route_class=AuthenticatedRoute)


class ResumeShoppingRequest(BaseModel):
    user_id: Any = Field(default=None, json_schema_extra={"deprecated": True})


@router.get("/sessions/{session_id}/shopping-checkpoint")
async def latest_shopping_checkpoint(
    session_id: str,
    user_id: str | None = Query(default=None, deprecated=True),
    container: AppContainer = Depends(get_container),
    identity: AuthIdentity = Depends(get_current_user),
) -> dict:
    async with container.session_factory() as db:
        await SessionRepository(db).reception(
            session_id, identity.user_id, idle_seconds=container.settings.reception_idle_seconds
        )
        row = await ShoppingCheckpointRepository(db).latest(session_id)
        await db.commit()
        snapshot = json.loads(row.snapshot_json)
        snapshot.pop("scope_key", None)
        return {
            "checkpoint_id": row.checkpoint_id,
            "status": row.status,
            "snapshot": snapshot,
            "parent_id": row.parent_id,
        }


@router.post("/shopping-checkpoints/{checkpoint_id}/resume", response_model=ChatResponse)
async def resume_shopping(
    checkpoint_id: str,
    payload: ResumeShoppingRequest,
    request: Request,
    container: AppContainer = Depends(get_container),
    identity: AuthIdentity = Depends(get_current_user),
) -> ChatResponse:
    async with container.session_factory() as db:
        row = await ShoppingCheckpointRepository(db).owned(checkpoint_id, identity.user_id)
        session_id = row.session_id
    return await container.chat_service.chat(
        ChatRequest(
            message="恢复选购查询",
            session_id=session_id,
            resume_checkpoint_id=checkpoint_id,
        ),
        trace_id=request.state.trace_id,
        auth=identity,
    )


def _reception_response(row, feedback=None) -> ReceptionResponse:
    return ReceptionResponse(
        session_id=row.session_id,
        status=row.status,
        end_reason=row.end_reason,
        resolved=row.end_reason == "resolved",
        last_activity_at=row.last_activity_at,
        closed_at=row.closed_at,
        feedback=ReceptionFeedbackResponse(
            score=feedback.score,
            resolved=feedback.resolved,
            reason=feedback.reason,
            last_assistant_message_id=feedback.last_assistant_message_id,
        )
        if feedback is not None
        else None,
    )


@router.get("/sessions/{session_id}/reception", response_model=ReceptionResponse)
async def reception_status(
    session_id: str,
    user_id: str | None = Query(default=None, deprecated=True),
    container: AppContainer = Depends(get_container),
    identity: AuthIdentity = Depends(get_current_user),
) -> ReceptionResponse:
    async with container.session_factory() as db:
        row = await SessionRepository(db).reception(
            session_id, identity.user_id, idle_seconds=container.settings.reception_idle_seconds
        )
        response = _reception_response(row, await SessionRepository(db).feedback(session_id))
        await db.commit()
        return response


@router.post("/sessions/{session_id}/close", response_model=ReceptionResponse)
async def close_reception(
    session_id: str,
    payload: CloseReceptionRequest,
    container: AppContainer = Depends(get_container),
    identity: AuthIdentity = Depends(get_current_user),
) -> ReceptionResponse:
    async with container.session_factory() as db:
        row = await SessionRepository(db).close_reception(
            session_id,
            identity.user_id,
            payload.reason,
            idle_seconds=container.settings.reception_idle_seconds,
        )
        response = _reception_response(row, await SessionRepository(db).feedback(session_id))
        await db.commit()
        return response


@router.post("/sessions/{session_id}/feedback", response_model=ReceptionFeedbackResponse)
async def submit_reception_feedback(
    session_id: str,
    payload: ReceptionFeedbackRequest,
    container: AppContainer = Depends(get_container),
    identity: AuthIdentity = Depends(get_current_user),
) -> ReceptionFeedbackResponse:
    async with container.session_factory() as db:
        try:
            row = await SessionRepository(db).submit_feedback(
                session_id, payload, user_id=identity.user_id, idle_seconds=container.settings.reception_idle_seconds
            )
            response = ReceptionFeedbackResponse(
                score=row.score,
                resolved=row.resolved,
                reason=row.reason,
                last_assistant_message_id=row.last_assistant_message_id,
            )
            await db.commit()
            return response
        except IntegrityError as exc:
            await db.rollback()
            raise AppError(
                "feedback already submitted; refresh reception",
                error_code=ErrorCode.INVALID_REQUEST,
                http_status=409,
            ) from exc


@router.post("/chat", response_model=ChatResponse)
async def chat(
    payload: ChatRequest,
    request: Request,
    container: AppContainer = Depends(get_container),
    identity: AuthIdentity = Depends(get_current_user),
) -> ChatResponse:
    """Answer one turn and return the full JSON response."""
    return await container.chat_service.chat(
        payload,
        trace_id=request.state.trace_id,
        auth=identity,
    )


@router.post("/chat/stream")
async def chat_stream(
    payload: ChatRequest,
    request: Request,
    container: AppContainer = Depends(get_container),
    identity: AuthIdentity = Depends(get_current_user),
) -> EventSourceResponse:
    """Answer one turn as an SSE stream of node, token and answer events."""
    # Reject closed/foreign sessions before StreamingResponse sends HTTP 200.
    if payload.session_id:
        async with container.session_factory() as db:
            await SessionRepository(db).ensure_active(
                payload.session_id,
                identity.user_id,
                idle_seconds=container.settings.reception_idle_seconds,
            )
            await db.commit()

    async def events():
        try:
            async for item in container.chat_service.stream(payload, trace_id=request.state.trace_id, auth=identity):
                yield JSONServerSentEvent(item)
        except AppError as exc:
            yield JSONServerSentEvent({"event": "error", "status_code": exc.http_status,
                "error_code": exc.error_code.value, "message": exc.message,
                "trace_id": request.state.trace_id}, event="error")

    return EventSourceResponse(events())
