"""Authenticate before FastAPI reads business bodies or opens an SSE stream."""

from fastapi.routing import APIRoute

from app.api.dependencies import resolve_admin, resolve_user
from app.core.data_scope import bind_data_scope, scope_from_identity
from app.core.exceptions import InvalidRequestError


class AuthenticatedRoute(APIRoute):
    admin = False

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def authenticated(request):
            if any(
                key.lower() in {"token", "access_token", "authorization", "refresh_token"}
                for key in request.query_params
            ):
                raise InvalidRequestError("credentials must be sent in Authorization header")
            request.state.auth_identity = await (
                resolve_admin(request) if self.admin else resolve_user(request)
            )
            with bind_data_scope(scope_from_identity(request.state.auth_identity)):
                response = await handler(request)
            response.headers["Cache-Control"] = "no-store"
            return response

        return authenticated


class AdminRoute(AuthenticatedRoute):
    admin = True
