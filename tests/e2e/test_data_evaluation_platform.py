from copy import deepcopy

from fastapi.testclient import TestClient

from app.config.settings import clear_settings_cache
from app.main import create_app


def dataset_payload():
    return {
        "name": "shopping",
        "version": "v1",
        "task": "shopping_update",
        "split": "regression",
        "source_type": "synthetic",
        "source": "test-authored",
        "license": "project-authored",
        "samples": [
            {
                "case_id": "add-white",
                "group_id": "shoe-color",
                "input": {"turns": ["推荐300元以内黑色42码鞋", "白色也可以"]},
                "expected": {
                    "constraints": {
                        "max_price_cents": 30000,
                        "colors": ["白色", "黑色"],
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


def test_data_review_freeze_evaluation_comparison_and_persistence(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "test-only-platform-token")
    clear_settings_cache()
    prefix = "/api/v1/admin/platform"
    headers = {"x-admin-token": "test-only-platform-token"}
    try:
        with TestClient(create_app()) as client:
            assert client.get(f"{prefix}/datasets").status_code == 401
            data = dataset_payload()
            created = client.post(f"{prefix}/datasets", json=data, headers=headers)
            assert created.status_code == 201
            dataset = created.json()
            dataset_id = dataset["dataset_id"]
            assert dataset["status"] == "draft"
            assert len(dataset["content_hash"]) == 64
            assert client.post(f"{prefix}/datasets", json=data, headers=headers).status_code == 409
            assert (
                client.post(
                    f"{prefix}/runs",
                    json={"dataset_id": dataset_id, "candidate_label": "current"},
                    headers=headers,
                ).status_code
                == 409
            )
            reviewed = client.post(
                f"{prefix}/datasets/{dataset_id}/review",
                json={
                    "decision": "approved",
                    "reviewer": "test-reviewer",
                    "note": "manually checked fixture gold",
                },
                headers=headers,
            )
            assert reviewed.status_code == 200
            assert reviewed.json()["content_hash"] == dataset["content_hash"]
            assert (
                client.post(
                    f"{prefix}/datasets/{dataset_id}/review",
                    json={
                        "decision": "rejected",
                        "reviewer": "second",
                        "note": "cannot overwrite",
                    },
                    headers=headers,
                ).status_code
                == 409
            )
            run = client.post(
                f"{prefix}/runs",
                json={"dataset_id": dataset_id, "candidate_label": "baseline"},
                headers=headers,
            ).json()
            assert run["status"] == "completed"
            assert run["report"]["summary"]["total"] == 1
            assert run["report"]["summary"]["passed"] == 1
            assert run["manifest"]["dataset_hash"] == dataset["content_hash"]
            assert run["manifest"]["external_api_calls"] == 0
            second = client.post(
                f"{prefix}/runs",
                json={"dataset_id": dataset_id, "candidate_label": "repeat"},
                headers=headers,
            ).json()
            comparison = client.get(
                f"{prefix}/compare",
                params={"baseline": run["run_id"], "candidate": second["run_id"]},
                headers=headers,
            ).json()
            assert comparison["improved"] == [] and comparison["regressed"] == []
            assert comparison["unchanged"] == 1
            another_split = {**data, "version": "v2", "split": "heldout"}
            assert (
                client.post(f"{prefix}/datasets", json=another_split, headers=headers).status_code
                == 409
            )
            revised = {**data, "version": "v2"}
            new_dataset = client.post(f"{prefix}/datasets", json=revised, headers=headers).json()
            client.post(
                f"{prefix}/datasets/{new_dataset['dataset_id']}/review",
                json={
                    "decision": "approved",
                    "reviewer": "reviewer",
                    "note": "new frozen version",
                },
                headers=headers,
            )
            third = client.post(
                f"{prefix}/runs",
                json={"dataset_id": new_dataset["dataset_id"], "candidate_label": "v2"},
                headers=headers,
            ).json()
            assert (
                client.get(
                    f"{prefix}/compare",
                    params={"baseline": run["run_id"], "candidate": third["run_id"]},
                    headers=headers,
                ).status_code
                == 409
            )
            malformed = deepcopy(data)
            malformed["version"] = "bad"
            malformed["samples"].append(deepcopy(malformed["samples"][0]))
            assert (
                client.post(f"{prefix}/datasets", json=malformed, headers=headers).status_code
                == 422
            )
        clear_settings_cache()
        with TestClient(create_app()) as client:
            restored = client.get(f"{prefix}/runs/{run['run_id']}", headers=headers).json()
            assert restored["report"]["summary"]["passed"] == 1
            assert (
                client.get(f"{prefix}/datasets/{dataset_id}", headers=headers).json()["status"]
                == "approved"
            )
    finally:
        clear_settings_cache()
