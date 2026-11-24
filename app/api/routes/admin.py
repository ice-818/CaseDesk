"""Admin routes for tool and skill introspection."""

from fastapi import APIRouter, Depends, Request

from app.api.auth_route import AdminRoute
from app.api.dependencies import get_container, resolve_admin
from app.core.exceptions import ForbiddenError
from app.schemas.auth import AuthIdentity
from app.services.container import AppContainer
from app.skills.loader import load_skills

router = APIRouter(prefix="/admin", tags=["admin"], route_class=AdminRoute)


async def require_admin(request: Request) -> AuthIdentity:
    identity = getattr(request.state, "auth_identity", None)
    identity = identity if identity is not None else await resolve_admin(request)
    if identity.role != "operator":
        raise ForbiddenError()
    return identity


@router.post("/users/{user_id}/disable", status_code=204)
async def disable_user(user_id: str, request: Request, identity: AuthIdentity = Depends(require_admin)):
    from starlette.responses import Response
    await request.app.state.container.auth_service.disable_user(user_id, identity)
    return Response(status_code=204)


@router.get("/tools", dependencies=[Depends(require_admin)])
async def list_tools(container: AppContainer = Depends(get_container)) -> dict:
    names = container.chat_service.registry.names()
    return {"tools": names, "count": len(names)}


@router.get("/skills", dependencies=[Depends(require_admin)])
async def list_skills() -> dict:
    return {"skills": load_skills()}
