"""FastAPI dependency providers."""

import hmac

from fastapi import Request

from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.schemas.auth import AuthIdentity
from app.services.container import AppContainer


def get_container(request: Request) -> AppContainer:
    return request.app.state.container


def get_auth_service(request: Request):
    return get_container(request).auth_service


def bearer_token(request: Request) -> str:
    values = request.headers.getlist("authorization")
    if len(values) != 1:
        raise UnauthorizedError()
    parts = values[0].split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise UnauthorizedError()
    return parts[1]


async def resolve_user(request: Request) -> AuthIdentity:
    return await get_auth_service(request).authenticate(bearer_token(request))


async def resolve_admin(request: Request) -> AuthIdentity:
    if "authorization" in request.headers:
        identity = await resolve_user(request)
        if identity.role != "operator":
            raise ForbiddenError()
        return identity
    settings = get_container(request).settings
    expected = settings.admin_api_token.get_secret_value()
    if "x-admin-token" in request.headers:
        values = request.headers.getlist("x-admin-token")
        if len(values) != 1 or not expected or not hmac.compare_digest(values[0].encode("utf-8"), expected.encode("utf-8")):
            raise UnauthorizedError()
        return AuthIdentity("system:admin-token", "operator", "admin_token")
    # Compatibility exception ONLY for admin routes. Invalid supplied
    # credentials never fall through here, and production never opens access.
    if settings.app_env in {"development", "testing"} and not expected:
        return AuthIdentity("system:dev-admin", "operator", "dev_admin")
    raise UnauthorizedError()


async def get_current_user(request: Request) -> AuthIdentity:
    identity = getattr(request.state, "auth_identity", None)
    if identity is None:
        identity = await resolve_user(request)
    if identity.source != "bearer":
        raise UnauthorizedError()
    return identity


def get_data_scope():
    from app.core.data_scope import require_data_scope
    return require_data_scope()
