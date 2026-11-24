"""Durable read-only shopping snapshots; recovery intentionally revalidates data."""

import json
import time
from datetime import UTC, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select

from app.agents.product_requirements import ProductRequirements
from app.core.data_scope import require_data_scope
from app.core.exceptions import AppError, ForbiddenError, ResourceNotFoundError
from app.core.time import utcnow
from app.repositories.models import SessionRow, ShoppingCheckpointRow
from app.repositories.scope_policy import session_condition
from app.repositories.session_repository import SessionRepository
from app.schemas.agent import ToolResult


class NavigationSnapshot(BaseModel):
    version: Literal[1]
    requirements: ProductRequirements
    loop: dict[str, Any]
    tool_results: list[ToolResult]
    step_count: int = Field(ge=0)
    elapsed_seconds: float = Field(default=0, ge=0, allow_inf_nan=False)
    scope_key: str


class ShoppingCheckpointRepository:
    def __init__(self, db):
        self.db = db
        self.scope = require_data_scope()

    def _query(self):
        return select(ShoppingCheckpointRow).join(SessionRow, ShoppingCheckpointRow.session_id == SessionRow.session_id).where(
            session_condition(self.scope), ShoppingCheckpointRow.user_id == SessionRow.user_id
        ).execution_options(populate_existing=True)

    async def _load(self, checkpoint_id, *, write=False):
        row = await self.db.scalar(self._query().where(ShoppingCheckpointRow.checkpoint_id == checkpoint_id))
        if row is None:
            if write:
                raise ForbiddenError()
            raise ResourceNotFoundError()
        return row

    async def latest(self, session_id):
        await SessionRepository(self.db).authorized(session_id)
        row = await self.db.scalar(self._query().where(ShoppingCheckpointRow.session_id == session_id).order_by(ShoppingCheckpointRow.updated_at.desc()).limit(1))
        if row is None:
            raise ResourceNotFoundError()
        return row

    async def latest_unfinished(self, session_id: str) -> NavigationSnapshot | None:
        """Read the newest non-completed snapshot visible to this scoped actor."""
        try:
            await SessionRepository(self.db).authorized(session_id)
        except ResourceNotFoundError:
            return None
        row = await self.db.scalar(
            self._query().where(
                ShoppingCheckpointRow.session_id == session_id,
                ShoppingCheckpointRow.status != "completed",
            ).order_by(ShoppingCheckpointRow.updated_at.desc()).limit(1)
        )
        return self._navigation_snapshot(row)

    async def recover_navigation(
        self, session_id: str, requirements: ProductRequirements
    ) -> tuple[str, NavigationSnapshot] | None:
        # Finished turns are the normal navigation source. Never skip a newer
        # running or incompatible turn to reuse an older set of recommendations.
        try:
            row = await self.latest(session_id)
        except ResourceNotFoundError:
            return None
        if row.status not in {"completed", "interrupted"}:
            return None
        snapshot = self._navigation_snapshot(row)
        if snapshot is None or not snapshot.loop:
            return None
        if snapshot.requirements.model_dump(exclude={"source_query"}) != requirements.model_dump(exclude={"source_query"}):
            return None
        return row.checkpoint_id, snapshot

    def _navigation_snapshot(self, row) -> NavigationSnapshot | None:
        if row is None:
            return None
        try:
            snapshot = NavigationSnapshot.model_validate_json(row.snapshot_json)
        except ValidationError:
            return None
        if snapshot.scope_key != self.scope.scope_key:
            return None
        return snapshot

    async def mark_interrupted(self, checkpoint_id):
        row = await self._load(checkpoint_id, write=True)
        row.status = "interrupted"
        row.updated_at = utcnow()
        await self.db.flush()

    async def save(self, state, *, status="running"):
        self.scope.assert_current()
        checkpoint_id = state.get("shopping_checkpoint_id")
        requirements = state.get("product_requirements")
        if not checkpoint_id or not requirements or not requirements.active:
            return
        session = await SessionRepository(self.db).authorized(state["session_id"], write=True)
        loop = state.get("shopping_loop") or {}
        started_at = loop.get("started_at")
        elapsed = time.monotonic() - started_at if started_at is not None else loop.get("elapsed_seconds", 0)
        snapshot = {
            "version": 1,
            "scope_key": self.scope.scope_key,
            "elapsed_seconds": max(0, elapsed),
            "requirements": requirements.model_dump(mode="json"),
            "tool_results": [r.model_dump(mode="json") for r in state.get("tool_results", [])],
            "loop": {
                k: v for k, v in loop.items() if k not in {"started_at", "elapsed_seconds"}
            },
            "step_count": state.get("step_count", 0),
        }
        row = await self.db.get(ShoppingCheckpointRow, checkpoint_id)
        if row is None:
            parent_id = state.get("resume_checkpoint_id")
            if parent_id:
                parent = await self._load(parent_id, write=True)
                if parent.session_id != session.session_id:
                    raise ForbiddenError()
            row = ShoppingCheckpointRow(
                checkpoint_id=checkpoint_id,
                session_id=state["session_id"],
                user_id=session.user_id,
                snapshot_json="",
                parent_id=state.get("resume_checkpoint_id"),
            )
            self.db.add(row)
        else:
            row = await self._load(checkpoint_id, write=True)
            if row.session_id != session.session_id:
                raise ForbiddenError()
        row.snapshot_json = json.dumps(snapshot, ensure_ascii=False)
        row.status = status
        row.updated_at = utcnow()
        await self.db.flush()

    async def owned(self, checkpoint_id, user_id=None):
        # Legacy user_id cannot select an authorization path.
        return await self._load(checkpoint_id)

    async def recover_requirements(self, checkpoint_id, user_id, session_id):
        row = await self.owned(checkpoint_id, user_id)
        timestamp = (
            row.updated_at.replace(tzinfo=UTC) if row.updated_at.tzinfo is None else row.updated_at
        )
        if row.session_id != session_id or row.status == "completed":
            raise AppError("checkpoint cannot be resumed", http_status=409)
        if row.status == "running" and utcnow() - timestamp < timedelta(seconds=60):
            raise AppError("execution may still be running; retry later", http_status=409)
        snapshot = json.loads(row.snapshot_json)
        if snapshot.get("version") != 1:
            raise AppError("unsupported checkpoint version", http_status=409)
        return ProductRequirements.model_validate(snapshot["requirements"])
