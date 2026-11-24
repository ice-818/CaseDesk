"""Stage 0 determinism guard: frozen fixtures must produce identical reports.

The same offline dataset is evaluated twice. Only timing fields (per-case
elapsed_ms and summary p95_ms) are allowed to differ; every verdict, actual
and expected value must be byte-for-byte stable across runs.
"""

import pytest

from app.schemas.platform import DatasetCreate
from app.services.offline_evaluation import evaluate

pytestmark = pytest.mark.determinism


def _search_dataset() -> dict:
    catalog = [
        {
            "sku": "A",
            "name": "鞋",
            "category": "鞋",
            "price_cents": 35000,
            "inventory": 1,
            "tags": ["color:黑色", "size:42"],
        },
        {
            "sku": "B",
            "name": "鞋",
            "category": "鞋",
            "price_cents": 29900,
            "inventory": 1,
            "tags": ["color:黑色", "size:42"],
        },
    ]
    return {
        "name": "determinism_search",
        "version": "v1",
        "task": "product_search",
        "split": "regression",
        "source_type": "synthetic",
        "source": "test-authored",
        "license": "project-authored",
        "samples": [
            {
                "case_id": "within_budget",
                "group_id": "budget",
                "input": {
                    "query": "鞋",
                    "catalog": catalog,
                    "filters": {"max_price_cents": 30000, "color": "黑色", "size": "42"},
                },
                "expected": ["B"],
            },
            {
                "case_id": "no_match",
                "group_id": "budget",
                "input": {
                    "query": "鞋",
                    "catalog": catalog,
                    "filters": {"max_price_cents": 20000, "color": "黑色", "size": "42"},
                },
                "expected": [],
            },
        ],
    }


def _update_dataset() -> dict:
    return {
        "name": "determinism_update",
        "version": "v1",
        "task": "shopping_update",
        "split": "regression",
        "source_type": "synthetic",
        "source": "test-authored",
        "license": "project-authored",
        "samples": [
            {
                "case_id": "single_turn_constraints",
                "group_id": "requirements",
                "input": {"turns": ["推荐300元以内黑色42码的鞋"]},
                "expected": {
                    "constraints": {
                        "max_price_cents": 30000,
                        "color": "黑色",
                        "size": "42",
                    },
                    "task_query": "鞋",
                    "active": True,
                    "clarification_required": False,
                    "evidence_tags": [],
                },
            }
        ],
    }


def _normalize(report: dict) -> dict:
    return {
        "summary": {key: value for key, value in report["summary"].items() if key != "p95_ms"},
        "cases": [
            {key: value for key, value in case.items() if key != "elapsed_ms"}
            for case in report["cases"]
        ],
    }


@pytest.mark.parametrize("payload_factory", [_search_dataset, _update_dataset])
@pytest.mark.asyncio
async def test_frozen_fixture_reports_are_identical_across_runs(payload_factory):
    payload = payload_factory()
    first = await evaluate(DatasetCreate.model_validate(payload))
    second = await evaluate(DatasetCreate.model_validate(payload))

    # Every case passes with no errors on both runs.
    for report in (first, second):
        assert report["summary"]["failed"] == 0
        assert report["summary"]["errors"] == 0
        assert report["summary"]["passed"] == report["summary"]["total"]

    # Ignoring timing, the two reports must be exactly equal.
    assert _normalize(first) == _normalize(second)
