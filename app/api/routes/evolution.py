"""Operator control plane for the semi-automatic self-evolution loop."""

from fastapi import APIRouter, Depends, Query

from app.api.auth_route import AdminRoute
from app.api.dependencies import get_container
from app.api.routes.admin import require_admin
from app.repositories.evolution_repository import EvolutionRepository
from app.schemas.evolution import (
    CreateProposalRequest,
    DatasetReviewRequest,
    IntentRegressionBundle,
    ProposalReviewRequest,
    RegressionRunRequest,
)
from app.services.container import AppContainer

router = APIRouter(
    prefix="/admin/evolution",
    tags=["self-evolution"],
    dependencies=[Depends(require_admin)],
    route_class=AdminRoute,
)


@router.post("/regression-datasets", status_code=201)
async def create_regression_dataset(
    payload: IntentRegressionBundle, container: AppContainer = Depends(get_container)
):
    return await EvolutionRepository(
        container.session_factory
    ).create_intent_regression_dataset(payload)


@router.post("/regression-datasets/{dataset_id}/review")
async def review_regression_dataset(
    dataset_id: str,
    payload: DatasetReviewRequest,
    container: AppContainer = Depends(get_container),
):
    return await EvolutionRepository(
        container.session_factory
    ).review_intent_regression_dataset(
        dataset_id, decision=payload.decision, note=payload.note
    )


@router.get("/proposals")
async def list_proposals(
    limit: int = Query(default=50, ge=1, le=100),
    container: AppContainer = Depends(get_container),
):
    return await EvolutionRepository(container.session_factory).list_proposals(limit=limit)


@router.post("/proposals", status_code=201)
async def create_proposal(
    payload: CreateProposalRequest, container: AppContainer = Depends(get_container)
):
    return await EvolutionRepository(container.session_factory).create_proposal(
        name=payload.name, version=payload.version
    )


@router.get("/proposals/{proposal_id}")
async def get_proposal(
    proposal_id: str, container: AppContainer = Depends(get_container)
):
    return await EvolutionRepository(container.session_factory).get_proposal(proposal_id)


@router.post("/proposals/{proposal_id}/attribute")
async def attribute_proposal(
    proposal_id: str, container: AppContainer = Depends(get_container)
):
    return await EvolutionRepository(container.session_factory).attribute_proposal(
        proposal_id
    )


@router.post("/proposals/{proposal_id}/regress")
async def run_regression(
    proposal_id: str,
    payload: RegressionRunRequest,
    container: AppContainer = Depends(get_container),
):
    return await EvolutionRepository(container.session_factory).run_regression(
        proposal_id,
        regression_dataset_id=payload.regression_dataset_id,
        candidate=payload.candidate,
    )


@router.post("/proposals/{proposal_id}/gate")
async def run_gate(
    proposal_id: str, container: AppContainer = Depends(get_container)
):
    return await EvolutionRepository(container.session_factory).run_gate(proposal_id)


@router.post("/proposals/{proposal_id}/review")
async def review_proposal(
    proposal_id: str,
    payload: ProposalReviewRequest,
    container: AppContainer = Depends(get_container),
):
    return await EvolutionRepository(container.session_factory).review_proposal(
        proposal_id, decision=payload.decision, note=payload.note
    )


@router.post("/proposals/{proposal_id}/promote")
async def promote_proposal(
    proposal_id: str, container: AppContainer = Depends(get_container)
):
    return await EvolutionRepository(container.session_factory).promote_proposal(
        proposal_id
    )
