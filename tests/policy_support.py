from contextlib import contextmanager

from app.core.data_scope import bind_data_scope, scope_from_identity
from app.retrieval.policy_scope import PolicyContext, bind_policy_context
from app.schemas.auth import AuthIdentity
from app.schemas.retrieval import RetrievedChunk


@contextmanager
def policy_case(
    role="service_agent", merchant="m_demo", fingerprint="test-corpus", category="apparel"
):
    scope = scope_from_identity(AuthIdentity("test-" + role, role, merchant_id=merchant))
    with bind_data_scope(scope):
        context = PolicyContext(scope, fingerprint, category=category)
        with bind_policy_context(context):
            yield context


def chunk(
    key="public",
    merchant=None,
    category=None,
    fingerprint="test-corpus",
    content="policy policy evidence",
):
    return RetrievedChunk(
        chunk_id=key,
        document_id=key,
        title=key,
        content=content,
        score=0,
        policy_layer="merchant" if merchant else ("category" if category else "platform"),
        merchant_id=merchant,
        applicable_category=category,
        policy_scope_version=1,
        corpus_fingerprint=fingerprint,
    )


def public_fixture_chunks(chunks):
    return [
        c.model_copy(
            update={
                "policy_layer": "platform",
                "policy_scope_version": 1,
                "corpus_fingerprint": "test-corpus",
            }
        )
        for c in chunks
    ]
