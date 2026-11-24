"""Request-local validated identity; never serialized into agent state."""

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from app.core.data_scope import assert_scope_identity
from app.core.exceptions import UnauthorizedError
from app.schemas.auth import AuthIdentity

_checker: ContextVar[Callable[[], Awaitable[AuthIdentity]] | None] = ContextVar(
    "auth_checker", default=None
)


@contextmanager
def bind_auth_checker(checker: Callable[[], Awaitable[AuthIdentity]]) -> Iterator[None]:
    token = _checker.set(checker)
    try:
        yield
    finally:
        _checker.reset(token)


async def check_execution_identity() -> AuthIdentity:
    checker = _checker.get()
    if checker is None:
        raise UnauthorizedError()
    identity = await checker()
    assert_scope_identity(identity)
    return identity
