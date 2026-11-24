"""Small shared data/evaluation control plane, protected by existing admin authorization."""


from fastapi import APIRouter, Depends, Query

from app.api.auth_route import AdminRoute
from app.api.dependencies import get_container
from app.api.routes.admin import require_admin
from app.repositories.platform_repository import PlatformRepository
from app.schemas.auth import AuthIdentity
from app.schemas.platform import DatasetCreate, DatasetReview, EvaluationCreate
from app.services.container import AppContainer

router = APIRouter(
    prefix="/admin/platform", tags=["data-evaluation"], dependencies=[Depends(require_admin)], route_class=AdminRoute
)






@router.get("/datasets")
async def datasets(
    limit: int = Query(default=50, ge=1, le=100), container: AppContainer = Depends(get_container)
):
    return await PlatformRepository(container.session_factory).datasets(limit=limit)


@router.post("/datasets", status_code=201)
async def create_dataset(payload: DatasetCreate, container: AppContainer = Depends(get_container)):
    return await PlatformRepository(container.session_factory).create_dataset(payload=payload)


@router.get("/datasets/{dataset_id}")
async def dataset_detail(dataset_id: str, container: AppContainer = Depends(get_container)):
    return await PlatformRepository(container.session_factory).dataset_detail(dataset_id=dataset_id)


@router.post("/datasets/{dataset_id}/review")
async def review_dataset(
    dataset_id: str, payload: DatasetReview, container: AppContainer = Depends(get_container),
    identity: AuthIdentity = Depends(require_admin),
):
    return await PlatformRepository(container.session_factory).review_dataset(dataset_id=dataset_id, payload=payload)


@router.get("/runs")
async def runs(
    limit: int = Query(default=50, ge=1, le=100), container: AppContainer = Depends(get_container)
):
    return await PlatformRepository(container.session_factory).runs(limit=limit)


@router.post("/runs", status_code=201)
async def create_run(payload: EvaluationCreate, container: AppContainer = Depends(get_container)):
    return await PlatformRepository(container.session_factory).create_run(payload=payload)


@router.get("/runs/{run_id}")
async def run_detail(run_id: str, container: AppContainer = Depends(get_container)):
    return await PlatformRepository(container.session_factory).run_detail(run_id=run_id)


@router.get("/compare")
async def compare(baseline: str, candidate: str, container: AppContainer = Depends(get_container)):
    return await PlatformRepository(container.session_factory).compare(baseline=baseline, candidate=candidate)
