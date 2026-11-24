"""Immutable, request-local authorization; never supplied by model/client arguments."""

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.schemas.auth import AuthIdentity, Role


@dataclass(frozen=True)
class DataScope:
    actor_user_id: str
    role: Role
    mode: Literal["platform", "shop"]
    merchant_id: str | None
    identity_source: Literal["bearer", "admin_token", "dev_admin"] = "bearer"

    def __post_init__(self) -> None:
        if not self.actor_user_id or self.role not in {"buyer", "service_agent", "operator"}:
            raise ForbiddenError()
        if self.role == "service_agent":
            if self.mode != "shop" or not self.merchant_id:
                raise ForbiddenError("service agent requires a merchant binding")
        elif self.mode != "platform" or self.merchant_id is not None:
            raise ForbiddenError()
        if self.identity_source != "bearer" and self.role != "operator":
            raise ForbiddenError()

    @property
    def scope_key(self) -> str:
        value = json.dumps(
            [self.actor_user_id, self.role, self.mode, self.merchant_id, self.identity_source],
            separators=(",", ":"),
        )
        return hashlib.sha256(value.encode()).hexdigest()

    def assert_current(self) -> None:
        if require_data_scope() != self:
            raise ForbiddenError("data scope changed")


def scope_from_identity(identity: AuthIdentity) -> DataScope:
    shop = identity.role == "service_agent"
    return DataScope(
        identity.user_id,
        identity.role,
        "shop" if shop else "platform",
        identity.merchant_id if shop else None,
        identity.source,
    )


_scope: ContextVar[DataScope | None] = ContextVar("data_scope", default=None)


def require_data_scope() -> DataScope:
    scope = _scope.get()
    if scope is None:
        raise UnauthorizedError("trusted data scope required")
    return scope


@contextmanager
def bind_data_scope(scope: DataScope) -> Iterator[None]:
    token = _scope.set(scope)
    try:
        yield
    finally:
        _scope.reset(token)


def assert_scope_identity(identity: AuthIdentity) -> None:
    if scope_from_identity(identity) != require_data_scope():
        raise ForbiddenError("identity role or merchant changed; start a new request")


def assert_tool_actor(user_id: str) -> DataScope:
    scope = require_data_scope()
    if scope.identity_source != "bearer" or scope.actor_user_id != user_id:
        raise ForbiddenError()
    return scope
