from types import SimpleNamespace

import pytest

from app.core.exceptions import ForbiddenError
from app.retrieval.vector import QdrantVectorStore, VectorRecord
from tests.policy_support import chunk, policy_case


class Client:
    def __init__(self):
        self.points = []
        self.calls = []
        self.down = False
        self.force = None

    def get_collections(self):
        return SimpleNamespace(collections=[])

    def create_collection(self, **kwargs):
        pass

    def upsert(self, *, collection_name, points):
        self.points = points
        self.collection = collection_name

    def search(self, **kwargs):
        self.calls.append(kwargs)
        if self.down:
            raise ConnectionError("offline")
        if self.force is not None:
            return self.force

        def matches(condition, payload):
            if hasattr(condition, "key"):
                match = condition.match
                return (
                    payload.get(condition.key) == match.value
                    if hasattr(match, "value")
                    else payload.get(condition.key) in match.any
                )
            return all(matches(c, payload) for c in condition.must or []) and (
                not condition.should or any(matches(c, payload) for c in condition.should)
            )

        # Simulates the server applying filter BEFORE its result limit.
        return [
            SimpleNamespace(payload=p.payload, score=1.0)
            for p in self.points
            if matches(kwargs["query_filter"], p.payload)
        ][: kwargs["limit"]]


def store():
    client = Client()
    store = QdrantVectorStore("unused", "policies", 2)
    store._client = client
    store.upsert(
        [
            VectorRecord(chunk("b", "m_second"), [100.0, 0.0]),
            VectorRecord(chunk("a", "m_demo"), [1.0, 0.0]),
        ]
    )
    return store, client


def test_payload_and_filter_are_scoped_before_server_limit():
    indexed, client = store()
    assert client.collection.endswith("_scope_v1")
    assert all(
        {
            "policy_layer",
            "merchant_id",
            "applicable_category",
            "policy_version",
            "policy_scope_version",
            "corpus_fingerprint",
        }
        <= p.payload.keys()
        for p in client.points
    )
    with policy_case():
        assert indexed.search([1.0, 0.0], top_k=1)[0].chunk_id == "a"
        assert client.calls[-1]["limit"] == 1
        serialized = client.calls[-1]["query_filter"].model_dump_json()
        assert "m_demo" in serialized and "m_second" not in serialized
    with policy_case(merchant="m_second"):
        assert indexed.search([1.0, 0.0], top_k=1)[0].chunk_id == "b"


def test_old_missing_payload_is_not_treated_as_public():
    indexed, client = store()
    client.force = [SimpleNamespace(payload={"chunk_id": "old", "content": "secret"}, score=1.0)]
    with policy_case():
        assert indexed.search([1.0, 0.0], top_k=1) == []


def test_foreign_new_payload_raises_and_does_not_degrade_unfiltered():
    indexed, client = store()
    client.force = [SimpleNamespace(payload=client.points[0].payload, score=1.0)]
    with policy_case(), pytest.raises(ForbiddenError):
        indexed.search([1.0, 0.0], top_k=1)


def test_network_fallback_uses_same_scope():
    indexed, client = store()
    client.down = True
    with policy_case():
        assert indexed.search([1.0, 0.0], top_k=1)[0].chunk_id == "a"
