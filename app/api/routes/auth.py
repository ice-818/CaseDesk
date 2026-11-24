"""Password login and database-backed revocation."""

from fastapi import APIRouter, Depends, Request, Response

from app.api.auth_route import AuthenticatedRoute
from app.api.dependencies import get_auth_service, get_current_user
from app.schemas.auth import AuthIdentity, LoginRequest, LoginResponse, PublicUser

router = APIRouter(prefix="/auth", tags=["auth"])
protected = APIRouter(route_class=AuthenticatedRoute)


@router.post("/login", response_model=LoginResponse)
async def login(payload: LoginRequest, request: Request, response: Response):
    result = await get_auth_service(request).login(
        payload.user_id,
        payload.password.get_secret_value(),
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return result


@protected.get("/me", response_model=PublicUser)
async def me(identity: AuthIdentity = Depends(get_current_user)):
    return identity.public()


@protected.post("/logout", status_code=204)
async def logout(request: Request, identity: AuthIdentity = Depends(get_current_user)):
    await get_auth_service(request).logout(identity)
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


router.include_router(protected)
