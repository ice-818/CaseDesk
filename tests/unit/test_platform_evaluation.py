from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.schemas.platform import DatasetCreate
from app.services.offline_evaluation import evaluate


def search_dataset():
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
        "name": "search",
        "version": "v1",
        "task": "product_search",
        "split": "regression",
        "source_type": "synthetic",
        "source": "test-authored",
        "license": "project-authored",
        "samples": [
            {
                "case_id": "budget",
                "group_id": "budget",
                "input": {
                    "query": "鞋",
                    "catalog": catalog,
                    "filters": {"max_price_cents": 30000, "color": "黑色", "size": "42"},
                },
                "expected": ["B"],
            }
        ],
    }


@pytest.mark.asyncio
async def test_real_database_evaluation_and_case_isolation():
    payload = search_dataset()
    second = deepcopy(payload["samples"][0])
    second["case_id"] = "no-match"
    second["group_id"] = "no-match"
    second["input"]["filters"]["max_price_cents"] = 20000
    second["expected"] = []
    payload["samples"].append(second)
    report = await evaluate(DatasetCreate.model_validate(payload))
    assert report["summary"]["passed"] == 2
    assert report["summary"]["errors"] == 0
    assert report["cases"][0]["actual"] == ["B"]


@pytest.mark.asyncio
async def test_report_does_not_hide_failed_cases():
    payload = search_dataset()
    payload["samples"][0]["expected"] = ["A"]  # Deliberately mismatched gold verifies reporting.
    report = await evaluate(DatasetCreate.model_validate(payload))
    assert report["summary"]["pass_rate"] == 0
    assert report["summary"]["failed"] == 1
    assert report["cases"][0]["actual"] == ["B"]
    assert report["cases"][0]["expected"] == ["A"]


def test_bad_source_and_duplicate_or_unknown_gold_are_rejected():
    for mutate in (
        lambda p: p.update(source=""),
        lambda p: p["samples"][0].update(expected=["missing"]),
        lambda p: p["samples"].append(deepcopy(p["samples"][0])),
    ):
        payload = search_dataset()
        mutate(payload)
        with pytest.raises(ValidationError):
            DatasetCreate.model_validate(payload)
