"""Chat orchestration: graph execution, streaming and persistence."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.graph import build_graph
from app.agents.navigation import parse_navigation
from app.agents.product_requirements import update_requirements
from app.agents.shopping_view import build_shopping_view
from app.agents.state import AgentState
from app.config.settings import Settings
from app.core.auth_context import bind_auth_checker
from app.core.data_scope import bind_data_scope, require_data_scope, scope_from_identity
from app.core.exceptions import (
    AppError,
    AuthenticationUnavailableError,
    ForbiddenError,
    GuardrailError,
    InvalidRequestError,
    RateLimitError,
    UnauthorizedError,
)
from app.core.ids import new_id
from app.core.request_scope import request_session
from app.infra.rate_limit import RateLimiter
from app.observability.metrics import REQUEST_LATENCY, REQUESTS, ROUTING, STAGE_LATENCY, TOKEN_USAGE
from app.providers.base import LLMProvider
from app.repositories.session_repository import SessionRepository
from app.repositories.shopping_checkpoint import ShoppingCheckpointRepository
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.policy_scope import authorize_policy_target, policy_request
from app.retrieval.reranker import DocumentReranker
from app.schemas.auth import AuthIdentity
from app.schemas.chat import ActionProposal, ChatCommand, ChatRequest, ChatResponse
from app.schemas.common import LatencyBreakdown, TokenUsage
from app.services.auth_service import AuthService
from app.tools.factory import build_tool_registry
from app.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PendingConfirmation:
    session_id: str
    user_id: str
    action: ActionProposal
    expires_at: float
    scope_key: str


class ChatService:
    def __init__(
        self,
        *,
        settings: Settings,
        llm: LLMProvider,
        retriever: HybridRetriever,
        reranker: DocumentReranker,
        session_factory: async_sessionmaker[AsyncSession],
        rate_limiter: RateLimiter,
        registry: ToolRegistry | None = None,
        auth_service: AuthService,
    ) -> None:
        self._settings = settings
        self._llm = llm
        self._retriever = retriever
        self._reranker = reranker
        self._session_factory = session_factory
        self._rate_limiter = rate_limiter
        self._auth = auth_service
        self.registry = registry or build_tool_registry(retriever)
        self._pending_actions: dict[str, PendingConfirmation] = {}
        self._graph = build_graph(
            llm=llm,
            settings=settings,
            retriever=retriever,
            reranker=reranker,
            registry=self.registry,
        )

    async def chat(self, request: ChatRequest, *, trace_id: str, auth: AuthIdentity) -> ChatResponse:
        identity = await self._auth.revalidate(auth)
        with bind_data_scope(scope_from_identity(identity)), policy_request():
            return await self._chat(request, trace_id=trace_id, auth=identity)

    async def _chat(self, request: ChatRequest, *, trace_id: str, auth: AuthIdentity) -> ChatResponse:
        identity = await self._auth.revalidate(auth)
        request = ChatCommand(**request.model_dump(exclude={"user_id"}), user_id=identity.user_id)
        await self._enforce_rate_limit(request.user_id)
        started = time.perf_counter()
        session_id = request.session_id or new_id("sess_")
        async with request_session(self._session_factory) as db:
            state = await self._initial_state(db, request, session_id, trace_id)
            try:
                result = await self._invoke_authenticated(state, identity)
            except (UnauthorizedError, ForbiddenError, AuthenticationUnavailableError):
                raise
            except Exception:
                if state.get("shopping_checkpoint_id"):
                    await db.rollback()
                    row = await ShoppingCheckpointRepository(db).owned(
                        state["shopping_checkpoint_id"], request.user_id
                    )
                    row.status = "interrupted"
                    await db.commit()
                raise
            await ShoppingCheckpointRepository(db).save(result, status="completed")
            response = self._to_response(result, session_id=session_id, trace_id=trace_id)
            await self._auth.revalidate(identity)
            await self._persist(db, request, response, summary=result.get("summary") or "")
            await db.commit()
        self._remember_pending(request, response)
        self._observe(response, started)
        return response

    async def stream(self, request: ChatRequest, *, trace_id: str, auth: AuthIdentity) -> AsyncIterator[dict]:
        identity = await self._auth.revalidate(auth)
        with bind_data_scope(scope_from_identity(identity)), policy_request():
            async for item in self._stream(request, trace_id=trace_id, auth=identity):
                yield item

    async def _stream(self, request: ChatRequest, *, trace_id: str, auth: AuthIdentity) -> AsyncIterator[dict]:
        """SSE: report progress, then send only the validated user-facing answer."""
        identity = await self._auth.revalidate(auth)
        request = ChatCommand(**request.model_dump(exclude={"user_id"}), user_id=identity.user_id)
        await self._enforce_rate_limit(request.user_id)
        started = time.perf_counter()
        session_id = request.session_id or new_id("sess_")
        async with request_session(self._session_factory) as db:
            state = await self._initial_state(db, request, session_id, trace_id)
            yield {
                "event": "started",
                "trace_id": trace_id,
                "session_id": session_id,
                "checkpoint_id": state.get("shopping_checkpoint_id"),
            }
            queue: asyncio.Queue[dict | None] = asyncio.Queue()
            holder: dict = {"merged": dict(state), "guardrail": None, "error": None}

            async def _run_graph() -> None:
                try:
                    async for update in self._authenticated_updates(state, identity):
                        for node, payload in update.items():
                            if isinstance(payload, dict):
                                holder["merged"].update(payload)
                            event: dict = {"event": "node", "node": node}
                            if isinstance(payload, dict) and payload.get("intent"):
                                event["intent"] = payload["intent"].label.value
                            if isinstance(payload, dict) and payload.get("strategy"):
                                event["strategy"] = payload["strategy"].value
                            await queue.put(event)
                except GuardrailError as exc:
                    holder["guardrail"] = exc
                except Exception as exc:  # graph failures surface after the queue drains
                    holder["error"] = exc
                    if state.get("shopping_checkpoint_id") and not isinstance(exc, UnauthorizedError | ForbiddenError | AuthenticationUnavailableError):
                        await db.rollback()
                        row = await ShoppingCheckpointRepository(db).owned(
                            state["shopping_checkpoint_id"], request.user_id
                        )
                        row.status = "interrupted"
                        await db.commit()
                finally:
                    await queue.put(None)

            task = asyncio.create_task(_run_graph())
            try:
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=self._settings.auth_stream_check_interval_seconds)
                    except TimeoutError:
                        await self._auth.revalidate(identity)
                        continue
                    await self._auth.revalidate(identity)
                    if item is None:
                        break
                    yield item
                await task
            finally:
                if not task.done():
                    task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

            if holder["guardrail"] is not None:
                exc = holder["guardrail"]
                await db.commit()
                yield {
                    "event": "answer",
                    "data": ChatResponse(
                        answer=exc.message,
                        session_id=session_id,
                        trace_id=trace_id,
                        refused=True,
                        error_code=exc.error_code.value,
                    ).model_dump(mode="json"),
                }
                yield {"event": "done", "trace_id": trace_id}
                return
            if holder["error"] is not None:
                raise holder["error"]
            merged: AgentState = holder["merged"]
            await ShoppingCheckpointRepository(db).save(merged, status="completed")
            response = self._to_response(merged, session_id=session_id, trace_id=trace_id)
            await self._auth.revalidate(identity)
            await self._persist(db, request, response, summary=merged.get("summary") or "")
            await db.commit()
        self._remember_pending(request, response)
        self._observe(response, started)
        # Never stream the LLM's JSON envelope or an answer that may still be
        # replaced by the faithfulness check. Only the final answer is public.
        for index in range(0, len(response.answer), 48):
            await self._auth.revalidate(identity)
            yield {"event": "token", "text": response.answer[index : index + 48]}
        await self._auth.revalidate(identity)
        yield {"event": "answer", "data": response.model_dump(mode="json")}
        yield {"event": "done", "trace_id": trace_id}

    async def _enforce_rate_limit(self, user_id: str) -> None:
        allowed = await self._rate_limiter.allow(f"user:{user_id}")
        if not allowed:
            raise RateLimitError()

    async def _initial_state(
        self,
        db: AsyncSession,
        request: ChatRequest,
        session_id: str,
        trace_id: str,
    ) -> AgentState:
        confirmed_action = self._resolve_confirmation(request, session_id)
        sessions = SessionRepository(db)
        await sessions.get_or_create(session_id, request.user_id)
        await authorize_policy_target(db, session_id=session_id)
        await sessions.ensure_active(
            session_id, request.user_id, idle_seconds=self._settings.reception_idle_seconds
        )
        history = await sessions.recent_messages(session_id)
        requirements = update_requirements(request.message, await sessions.shopping_task(session_id))
        navigation = parse_navigation(request.message)
        is_navigation = (
            navigation.command != "unknown" and not request.resume_checkpoint_id and not confirmed_action
        )
        recovered_navigation = None
        if is_navigation and requirements.active:
            recovered_navigation = await ShoppingCheckpointRepository(db).recover_navigation(
                session_id, requirements
            )
        if request.resume_checkpoint_id:
            if request.confirm_action_id or confirmed_action:
                raise InvalidRequestError("shopping recovery cannot execute confirmed writes")
            saved = await ShoppingCheckpointRepository(db).recover_requirements(
                request.resume_checkpoint_id, request.user_id, session_id
            )
            current = await sessions.shopping_task(session_id)
            if current is None or current.model_dump() != saved.model_dump():
                raise AppError("shopping requirements changed; start a new query", http_status=409)
            requirements = saved
            request.message = saved.source_query
        if not is_navigation or recovered_navigation:
            await sessions.save_shopping_task(session_id, requirements)
        state: AgentState = {
            "trace_id": trace_id,
            "session_id": session_id,
            "user_id": request.user_id,
            "user_query": request.message,
            "product_requirements": requirements,
            "messages": history,
            "summary": "",
            "retrieved_chunks": [],
            "tool_results": [],
            "citations": [],
            "refused": False,
            "step_count": 0,
            "token_usage": TokenUsage(),
            "latency": LatencyBreakdown(),
            "confirm_action_id": request.confirm_action_id,
            "confirmed_action": confirmed_action,
            "idempotency_key": request.idempotency_key,
            "shopping_checkpoint_id": new_id("shop_") if requirements.active and not is_navigation else None,
            "resume_checkpoint_id": request.resume_checkpoint_id,
        }
        if is_navigation:
            state["navigation"] = navigation
        if recovered_navigation:
            parent_id, snapshot = recovered_navigation
            state["shopping_checkpoint_id"] = new_id("shop_")
            state["resume_checkpoint_id"] = parent_id
            state["shopping_loop"] = {**snapshot.loop, "elapsed_seconds": snapshot.elapsed_seconds}
            state["tool_results"] = snapshot.tool_results
            state["step_count"] = snapshot.step_count
        if state["shopping_checkpoint_id"]:
            await ShoppingCheckpointRepository(db).save(state)
        # Release initial session/task writes before long model work so logout
        # on another connection is not blocked by a SQLite write transaction.
        await db.commit()
        return state

    def _resolve_confirmation(self, request: ChatRequest, session_id: str) -> ActionProposal | None:
        if not request.confirm_action_id:
            return None
        pending = self._pending_actions.get(request.confirm_action_id)
        if (
            pending is None
            or pending.expires_at < time.monotonic()
            or pending.session_id != session_id
            or pending.user_id != request.user_id
            or pending.scope_key != require_data_scope().scope_key
        ):
            raise InvalidRequestError("待确认操作不存在或已过期，请重新发起。")
        return pending.action

    def _remember_pending(self, request: ChatRequest, response: ChatResponse) -> None:
        now = time.monotonic()
        self._pending_actions = {
            key: value for key, value in self._pending_actions.items() if value.expires_at > now
        }
        action = response.pending_action
        if action is not None:
            self._pending_actions[action.action_id] = PendingConfirmation(
                session_id=response.session_id,
                user_id=request.user_id,
                action=action,
                expires_at=now + 15 * 60,
                scope_key=require_data_scope().scope_key,
            )

    async def _invoke_authenticated(self, state: AgentState, identity: AuthIdentity) -> AgentState:
        async def check():
            return await self._auth.revalidate(identity)
        with bind_auth_checker(check):
            task = asyncio.create_task(self._invoke(state))
            try:
                while not task.done():
                    await asyncio.wait({task}, timeout=self._settings.auth_stream_check_interval_seconds)
                    await check()
                return await task
            finally:
                if not task.done():
                    task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    async def _authenticated_updates(self, state: AgentState, identity: AuthIdentity):
        async def check():
            return await self._auth.revalidate(identity)
        with bind_auth_checker(check):
            async for update in self._graph.astream(state, stream_mode="updates"):
                await check()
                yield update

    async def _invoke(self, state: AgentState) -> AgentState:
        try:
            # LangGraph widens the state to dict[str, Any]; the graph contract is AgentState.
            return cast(AgentState, await self._graph.ainvoke(state))
        except GuardrailError as exc:
            return {
                **state,
                "final_answer": exc.message,
                "refused": True,
                "error": exc.error_code.value,
            }
        except AppError:
            raise

    async def _persist(
        self,
        db: AsyncSession,
        request: ChatRequest,
        response: ChatResponse,
        *,
        summary: str = "",
    ) -> None:
        sessions = SessionRepository(db)
        await sessions.append_message(response.session_id, "user", request.message)
        await sessions.append_message(response.session_id, "assistant", response.answer)
        if summary:
            await sessions.save_summary(response.session_id, summary)

    def _to_response(
        self,
        result: AgentState,
        *,
        session_id: str,
        trace_id: str,
    ) -> ChatResponse:
        answer = result.get("final_answer") or "暂时无法回答，请稍后重试。"
        strategy = result.get("strategy")
        intent = result.get("intent")
        return ChatResponse(
            checkpoint_id=result.get("shopping_checkpoint_id"),
            shopping_view=build_shopping_view(result.get("shopping_loop"), result.get("product_requirements")),
            answer=answer,
            session_id=session_id,
            trace_id=trace_id,
            intent=intent.label if intent else None,
            complexity=result.get("query_complexity"),
            strategy=strategy,
            citations=result.get("citations") or [],
            pending_action=result.get("pending_action"),
            refused=bool(result.get("refused")),
            error_code=result.get("error"),
            token_usage=result.get("token_usage") or TokenUsage(),
            latency=result.get("latency") or LatencyBreakdown(),
        )

    def _observe(self, response: ChatResponse, started: float) -> None:
        total_ms = (time.perf_counter() - started) * 1000
        response.latency.total_ms = total_ms
        REQUESTS.labels(
            strategy=(response.strategy.value if response.strategy else "unknown"),
            intent=(response.intent.value if response.intent else "unknown"),
            status="ok" if not response.refused else "refused",
        ).inc()
        if response.strategy:
            ROUTING.labels(strategy=response.strategy.value).inc()
        REQUEST_LATENCY.observe(total_ms / 1000)
        STAGE_LATENCY.labels(stage="intent").observe(response.latency.intent_ms / 1000)
        STAGE_LATENCY.labels(stage="retrieval").observe(response.latency.retrieval_ms / 1000)
        STAGE_LATENCY.labels(stage="llm").observe(response.latency.llm_ms / 1000)
        TOKEN_USAGE.labels(kind="total").inc(response.token_usage.total_tokens)
        logger.info(
            "chat_completed",
            extra={
                "trace_id": response.trace_id,
                "strategy": response.strategy.value if response.strategy else None,
                "intent": response.intent.value if response.intent else None,
                "latency_ms": round(total_ms, 1),
            },
        )
