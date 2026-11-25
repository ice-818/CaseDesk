from fastapi.testclient import TestClient

from app.config.settings import clear_settings_cache
from app.main import create_app

EVO = "/api/v1/admin/evolution"
ADMIN_HEADERS = {"x-admin-token": "test-evolution-token"}


def _seed_low_score_feedback(
    client, reason: str, buyer_headers: dict, message: str = "你好"
) -> None:
    chat = client.post(
        "/api/v1/chat",
        json={"message": message, "user_id": "owner"},
        headers=buyer_headers,
    )
    assert chat.status_code == 200
    session_id = chat.json()["session_id"]
    closed = client.post(
        f"/api/v1/sessions/{session_id}/close",
        json={"user_id": "owner", "reason": "resolved"},
        headers=buyer_headers,
    )
    assert closed.status_code == 200
    feedback = client.post(
        f"/api/v1/sessions/{session_id}/feedback",
        json={
            "user_id": "owner",
            "score": 2,
            "resolved": False,
            "reason": reason,
        },
        headers=buyer_headers,
    )
    assert feedback.status_code == 200


def test_full_human_in_the_loop_evolution_pipeline(tmp_path, monkeypatch, auth_headers):
    monkeypatch.setenv("ADMIN_API_TOKEN", "test-evolution-token")
    clear_settings_cache()
    buyer_headers = auth_headers("owner")
    try:
        with TestClient(create_app()) as client:
            _seed_low_score_feedback(
                client, "答非所问，理解错我的意思了", buyer_headers
            )

            regression_bundle = {
                "name": "intent_reg",
                "version": "v1",
                "cases": [
                    {"case_id": "c1", "query": "我的快递到哪了", "expected_intent": "logistics"},
                    {"case_id": "c2", "query": "催发货", "expected_intent": "logistics"},
                ],
            }
            created_reg = client.post(
                f"{EVO}/regression-datasets", json=regression_bundle, headers=ADMIN_HEADERS
            )
            assert created_reg.status_code == 201
            reg_id = created_reg.json()["dataset_id"]
            reviewed_reg = client.post(
                f"{EVO}/regression-datasets/{reg_id}/review",
                json={"decision": "approved", "note": "frozen intent fixtures"},
                headers=ADMIN_HEADERS,
            )
            assert reviewed_reg.status_code == 200

            proposal = client.post(
                f"{EVO}/proposals",
                json={"name": "fb_prop", "version": "v1"},
                headers=ADMIN_HEADERS,
            )
            assert proposal.status_code == 201
            prop_id = proposal.json()["proposal_id"]
            assert proposal.json()["status"] == "collect"

            attributed = client.post(
                f"{EVO}/proposals/{prop_id}/attribute", headers=ADMIN_HEADERS
            ).json()
            assert attributed["status"] == "attribute"
            assert attributed["attribution"]["counts"]["intent_error"] == 1

            regressed = client.post(
                f"{EVO}/proposals/{prop_id}/regress",
                json={
                    "regression_dataset_id": reg_id,
                    "candidate": {
                        "type": "intent_keyword",
                        "intent": "logistics",
                        "keyword": "催发货",
                    },
                },
                headers=ADMIN_HEADERS,
            ).json()
            assert regressed["status"] == "regress"
            assert regressed["baseline_run_id"] != regressed["candidate_run_id"]

            gate = client.post(
                f"{EVO}/proposals/{prop_id}/gate", headers=ADMIN_HEADERS
            ).json()
            assert gate["status"] == "gate"
            assert gate["gate"]["passed"] is True
            assert gate["gate"]["improved"] == ["c2"]
            assert gate["gate"]["regressed"] == []
            assert gate["gate"]["candidate_pass_rate"] == 1.0

            reviewed = client.post(
                f"{EVO}/proposals/{prop_id}/review",
                json={"decision": "approved", "note": "deterministic keyword fix verified"},
                headers=ADMIN_HEADERS,
            ).json()
            assert reviewed["status"] == "review"
            assert reviewed["review_decision"] == "approved"

            promoted = client.post(
                f"{EVO}/proposals/{prop_id}/promote", headers=ADMIN_HEADERS
            ).json()
            assert promoted["status"] == "promoted"
    finally:
        clear_settings_cache()


def test_gate_blocks_candidate_that_regresses_frozen_cases(
    tmp_path, monkeypatch, auth_headers
):
    monkeypatch.setenv("ADMIN_API_TOKEN", "test-evolution-token")
    clear_settings_cache()
    buyer_headers = auth_headers("owner")
    try:
        with TestClient(create_app()) as client:
            _seed_low_score_feedback(client, "没有解决问题", buyer_headers)

            regression_bundle = {
                "name": "intent_reg",
                "version": "v1",
                "cases": [
                    {"case_id": "o1", "query": "我的订单", "expected_intent": "order_inquiry"},
                ],
            }
            reg_id = client.post(
                f"{EVO}/regression-datasets", json=regression_bundle, headers=ADMIN_HEADERS
            ).json()["dataset_id"]
            client.post(
                f"{EVO}/regression-datasets/{reg_id}/review",
                json={"decision": "approved", "note": "freeze"},
                headers=ADMIN_HEADERS,
            )
            prop_id = client.post(
                f"{EVO}/proposals",
                json={"name": "bad_prop", "version": "v1"},
                headers=ADMIN_HEADERS,
            ).json()["proposal_id"]
            client.post(f"{EVO}/proposals/{prop_id}/attribute", headers=ADMIN_HEADERS)
            client.post(
                f"{EVO}/proposals/{prop_id}/regress",
                json={
                    "regression_dataset_id": reg_id,
                    "candidate": {
                        "type": "intent_keyword",
                        "intent": "human_handoff",
                        "keyword": "订单",
                    },
                },
                headers=ADMIN_HEADERS,
            )
            gate = client.post(
                f"{EVO}/proposals/{prop_id}/gate", headers=ADMIN_HEADERS
            ).json()
            assert gate["status"] == "blocked"
            assert gate["gate"]["passed"] is False
            assert gate["gate"]["regressed"] == ["o1"]
            # A blocked proposal cannot reach human review or be promoted.
            assert (
                client.post(
                    f"{EVO}/proposals/{prop_id}/review",
                    json={"decision": "approved", "note": "cannot bypass"},
                    headers=ADMIN_HEADERS,
                ).status_code
                == 409
            )
            assert (
                client.post(
                    f"{EVO}/proposals/{prop_id}/promote", headers=ADMIN_HEADERS
                ).status_code
                == 409
            )
    finally:
        clear_settings_cache()


def test_requires_operator_and_rejects_illegal_transitions(
    tmp_path, monkeypatch, auth_headers
):
    monkeypatch.setenv("ADMIN_API_TOKEN", "test-evolution-token")
    clear_settings_cache()
    buyer_headers = auth_headers("owner")
    try:
        with TestClient(create_app()) as client:
            _seed_low_score_feedback(client, "不满意", buyer_headers)
            # Buyer identity cannot reach the operator control plane.
            assert client.get(f"{EVO}/proposals", headers=buyer_headers).status_code == 403

            prop_id = client.post(
                f"{EVO}/proposals",
                json={"name": "step_prop", "version": "v1"},
                headers=ADMIN_HEADERS,
            ).json()["proposal_id"]
            # Skipping collect/attribute straight to gate is rejected.
            assert (
                client.post(
                    f"{EVO}/proposals/{prop_id}/gate", headers=ADMIN_HEADERS
                ).status_code
                == 409
            )
            # A missing proposal is a 404.
            assert (
                client.get(
                    f"{EVO}/proposals/prop_does_not_exist", headers=ADMIN_HEADERS
                ).status_code
                == 404
            )
    finally:
        clear_settings_cache()


def test_regression_dataset_review_guardrails(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "test-evolution-token")
    clear_settings_cache()
    try:
        with TestClient(create_app()) as client:
            assert (
                client.post(
                    f"{EVO}/regression-datasets/ds_missing/review",
                    json={"decision": "approved", "note": "x"},
                    headers=ADMIN_HEADERS,
                ).status_code
                == 404
            )
            bundle = {
                "name": "g",
                "version": "v1",
                "cases": [
                    {"case_id": "a", "query": "快递", "expected_intent": "logistics"}
                ],
            }
            ds_id = client.post(
                f"{EVO}/regression-datasets", json=bundle, headers=ADMIN_HEADERS
            ).json()["dataset_id"]
            client.post(
                f"{EVO}/regression-datasets/{ds_id}/review",
                json={"decision": "approved", "note": "first"},
                headers=ADMIN_HEADERS,
            )
            # A reviewed dataset cannot be re-reviewed.
            assert (
                client.post(
                    f"{EVO}/regression-datasets/{ds_id}/review",
                    json={"decision": "rejected", "note": "second"},
                    headers=ADMIN_HEADERS,
                ).status_code
                == 409
            )
    finally:
        clear_settings_cache()


def test_run_regression_requires_existing_approved_dataset(
    tmp_path, monkeypatch, auth_headers
):
    monkeypatch.setenv("ADMIN_API_TOKEN", "test-evolution-token")
    clear_settings_cache()
    buyer_headers = auth_headers("owner")
    try:
        with TestClient(create_app()) as client:
            _seed_low_score_feedback(client, "没解决", buyer_headers)
            prop_id = client.post(
                f"{EVO}/proposals",
                json={"name": "p", "version": "v1"},
                headers=ADMIN_HEADERS,
            ).json()["proposal_id"]
            client.post(f"{EVO}/proposals/{prop_id}/attribute", headers=ADMIN_HEADERS)
            body = {
                "regression_dataset_id": "ds_missing",
                "candidate": {
                    "type": "intent_keyword",
                    "intent": "logistics",
                    "keyword": "x",
                },
            }
            assert (
                client.post(
                    f"{EVO}/proposals/{prop_id}/regress",
                    json=body,
                    headers=ADMIN_HEADERS,
                ).status_code
                == 404
            )
            ds_id = client.post(
                f"{EVO}/regression-datasets",
                json={
                    "name": "g",
                    "version": "v1",
                    "cases": [
                        {"case_id": "a", "query": "快递", "expected_intent": "logistics"}
                    ],
                },
                headers=ADMIN_HEADERS,
            ).json()["dataset_id"]
            # A draft (not yet approved) dataset cannot run regression.
            body["regression_dataset_id"] = ds_id
            assert (
                client.post(
                    f"{EVO}/proposals/{prop_id}/regress",
                    json=body,
                    headers=ADMIN_HEADERS,
                ).status_code
                == 409
            )
    finally:
        clear_settings_cache()


def test_human_rejection_terminates_proposal(tmp_path, monkeypatch, auth_headers):
    monkeypatch.setenv("ADMIN_API_TOKEN", "test-evolution-token")
    clear_settings_cache()
    buyer_headers = auth_headers("owner")
    try:
        with TestClient(create_app()) as client:
            _seed_low_score_feedback(client, "答非所问", buyer_headers)
            ds_id = client.post(
                f"{EVO}/regression-datasets",
                json={
                    "name": "g",
                    "version": "v1",
                    "cases": [
                        {"case_id": "a", "query": "快递", "expected_intent": "logistics"},
                        {"case_id": "b", "query": "催发货", "expected_intent": "logistics"},
                    ],
                },
                headers=ADMIN_HEADERS,
            ).json()["dataset_id"]
            client.post(
                f"{EVO}/regression-datasets/{ds_id}/review",
                json={"decision": "approved", "note": "ok"},
                headers=ADMIN_HEADERS,
            )
            prop_id = client.post(
                f"{EVO}/proposals",
                json={"name": "p", "version": "v1"},
                headers=ADMIN_HEADERS,
            ).json()["proposal_id"]
            client.post(f"{EVO}/proposals/{prop_id}/attribute", headers=ADMIN_HEADERS)
            client.post(
                f"{EVO}/proposals/{prop_id}/regress",
                json={
                    "regression_dataset_id": ds_id,
                    "candidate": {
                        "type": "intent_keyword",
                        "intent": "logistics",
                        "keyword": "催发货",
                    },
                },
                headers=ADMIN_HEADERS,
            )
            client.post(f"{EVO}/proposals/{prop_id}/gate", headers=ADMIN_HEADERS)
            rejected = client.post(
                f"{EVO}/proposals/{prop_id}/review",
                json={"decision": "rejected", "note": "not confident"},
                headers=ADMIN_HEADERS,
            ).json()
            assert rejected["status"] == "rejected"
            assert rejected["review_decision"] == "rejected"
            # A rejected proposal cannot be promoted.
            assert (
                client.post(
                    f"{EVO}/proposals/{prop_id}/promote", headers=ADMIN_HEADERS
                ).status_code
                == 409
            )
    finally:
        clear_settings_cache()


def test_consolidate_feedback_into_new_regression_version(
    tmp_path, monkeypatch, auth_headers
):
    monkeypatch.setenv("ADMIN_API_TOKEN", "test-evolution-token")
    clear_settings_cache()
    buyer_headers = auth_headers("owner")
    try:
        with TestClient(create_app()) as client:
            # The user message contains the proposed keyword.
            _seed_low_score_feedback(
                client, "答非所问", buyer_headers, message="催发货"
            )
            regression_bundle = {
                "name": "intent_reg",
                "version": "v1",
                "cases": [
                    {"case_id": "c1", "query": "我的快递到哪了", "expected_intent": "logistics"},
                ],
            }
            reg_id = client.post(
                f"{EVO}/regression-datasets", json=regression_bundle, headers=ADMIN_HEADERS
            ).json()["dataset_id"]
            client.post(
                f"{EVO}/regression-datasets/{reg_id}/review",
                json={"decision": "approved", "note": "freeze"},
                headers=ADMIN_HEADERS,
            )
            prop_id = client.post(
                f"{EVO}/proposals",
                json={"name": "fb_prop", "version": "v1"},
                headers=ADMIN_HEADERS,
            ).json()["proposal_id"]
            client.post(f"{EVO}/proposals/{prop_id}/attribute", headers=ADMIN_HEADERS)
            client.post(
                f"{EVO}/proposals/{prop_id}/regress",
                json={
                    "regression_dataset_id": reg_id,
                    "candidate": {
                        "type": "intent_keyword",
                        "intent": "logistics",
                        "keyword": "催发货",
                    },
                },
                headers=ADMIN_HEADERS,
            )
            client.post(f"{EVO}/proposals/{prop_id}/gate", headers=ADMIN_HEADERS)
            client.post(
                f"{EVO}/proposals/{prop_id}/review",
                json={"decision": "approved", "note": "ok"},
                headers=ADMIN_HEADERS,
            )
            client.post(f"{EVO}/proposals/{prop_id}/promote", headers=ADMIN_HEADERS)

            consolidated = client.post(
                f"{EVO}/proposals/{prop_id}/consolidate",
                json={"new_version": "v2"},
                headers=ADMIN_HEADERS,
            )
            assert consolidated.status_code == 200
            data = consolidated.json()
            assert data["status"] == "draft"
            assert data["version"] == "v2"
            cases = data["payload"]["cases"]
            assert len(cases) == 2
            assert cases[0]["case_id"] == "c1"  # Original case retained.
            new_case = cases[1]
            assert new_case["query"] == "催发货"
            assert new_case["expected_intent"] == "logistics"
            # The sediment version still needs human approval to become frozen.
            approved = client.post(
                f"{EVO}/regression-datasets/{data['dataset_id']}/review",
                json={"decision": "approved", "note": "confirmed sediment"},
                headers=ADMIN_HEADERS,
            )
            assert approved.status_code == 200
    finally:
        clear_settings_cache()


def test_consolidate_requires_promoted_and_dedupes_query(
    tmp_path, monkeypatch, auth_headers
):
    monkeypatch.setenv("ADMIN_API_TOKEN", "test-evolution-token")
    clear_settings_cache()
    buyer_headers = auth_headers("owner")
    try:
        with TestClient(create_app()) as client:
            _seed_low_score_feedback(
                client, "答非所问", buyer_headers, message="催发货"
            )
            reg_id = client.post(
                f"{EVO}/regression-datasets",
                json={
                    "name": "g",
                    "version": "v1",
                    "cases": [
                        {"case_id": "c1", "query": "催发货", "expected_intent": "logistics"},
                    ],
                },
                headers=ADMIN_HEADERS,
            ).json()["dataset_id"]
            client.post(
                f"{EVO}/regression-datasets/{reg_id}/review",
                json={"decision": "approved", "note": "freeze"},
                headers=ADMIN_HEADERS,
            )
            prop_id = client.post(
                f"{EVO}/proposals",
                json={"name": "p", "version": "v1"},
                headers=ADMIN_HEADERS,
            ).json()["proposal_id"]
            client.post(f"{EVO}/proposals/{prop_id}/attribute", headers=ADMIN_HEADERS)
            # A non-promoted proposal cannot be consolidated.
            assert (
                client.post(
                    f"{EVO}/proposals/{prop_id}/consolidate",
                    json={"new_version": "v2"},
                    headers=ADMIN_HEADERS,
                ).status_code
                == 409
            )
            client.post(
                f"{EVO}/proposals/{prop_id}/regress",
                json={
                    "regression_dataset_id": reg_id,
                    "candidate": {
                        "type": "intent_keyword",
                        "intent": "logistics",
                        "keyword": "催发货",
                    },
                },
                headers=ADMIN_HEADERS,
            )
            client.post(f"{EVO}/proposals/{prop_id}/gate", headers=ADMIN_HEADERS)
            client.post(
                f"{EVO}/proposals/{prop_id}/review",
                json={"decision": "approved", "note": "ok"},
                headers=ADMIN_HEADERS,
            )
            client.post(f"{EVO}/proposals/{prop_id}/promote", headers=ADMIN_HEADERS)
            # The regression set already contains the same query: nothing new.
            assert (
                client.post(
                    f"{EVO}/proposals/{prop_id}/consolidate",
                    json={"new_version": "v2"},
                    headers=ADMIN_HEADERS,
                ).status_code
                == 409
            )
    finally:
        clear_settings_cache()


def test_evolution_list_endpoints_expose_datasets_and_feedback_cases(
    tmp_path, monkeypatch, auth_headers
):
    monkeypatch.setenv("ADMIN_API_TOKEN", "test-evolution-token")
    clear_settings_cache()
    buyer_headers = auth_headers("owner")
    try:
        with TestClient(create_app()) as client:
            _seed_low_score_feedback(
                client, "答非所问", buyer_headers, message="催发货"
            )
            reg_id = client.post(
                f"{EVO}/regression-datasets",
                json={
                    "name": "g",
                    "version": "v1",
                    "cases": [
                        {"case_id": "a", "query": "催发货", "expected_intent": "logistics"}
                    ],
                },
                headers=ADMIN_HEADERS,
            ).json()["dataset_id"]
            prop_id = client.post(
                f"{EVO}/proposals",
                json={"name": "p", "version": "v1"},
                headers=ADMIN_HEADERS,
            ).json()["proposal_id"]

            datasets = client.get(
                f"{EVO}/regression-datasets", headers=ADMIN_HEADERS
            )
            assert datasets.status_code == 200
            assert reg_id in [d["dataset_id"] for d in datasets.json()["datasets"]]

            feedback = client.get(
                f"{EVO}/proposals/{prop_id}/feedback-cases", headers=ADMIN_HEADERS
            )
            assert feedback.status_code == 200
            body = feedback.json()
            assert body["dataset_id"]
            assert len(body["cases"]) == 1
            assert body["cases"][0]["turns"][0]["content"] == "催发货"
    finally:
        clear_settings_cache()
