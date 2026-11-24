import pytest

from app.config.settings import Settings
from app.core.exceptions import ForbiddenError
from app.providers.base import LLMResponse
from app.retrieval.bm25 import BM25Retriever
from app.retrieval.cache import MemoryRetrievalCache
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.reranker import DocumentReranker
from app.retrieval.self_rag import SelfRAGLoop
from app.retrieval.vector import InMemoryVectorStore, VectorRecord, VectorRetriever
from tests.policy_support import chunk, policy_case


class Embedder:
    async def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]


class Reranker:
    async def rerank(self, query, documents, *, top_n):
        return [(i, 1.0) for i in range(min(top_n, len(documents)))]


class Rewriter:
    async def rewrite(self, query, **kwargs):
        return [query]


class Grade:
    def __init__(self):
        self.calls = 0

    async def generate(self, messages, **kwargs):
        self.calls += 1
        assert all("foreign-secret" not in m["content"] for m in messages)
        return LLMResponse(
            content='{"relevant":true,"sufficient":'
            + ("true" if self.calls >= 2 else "false")
            + ',"missing_aspect":"detail","rationale":"test"}',
            model="fake",
        )


def components():
    docs = [
        chunk("foreign", "m_second", content="foreign-secret policy policy policy"),
        chunk("own", "m_demo"),
        chunk("global"),
    ]
    bm = BM25Retriever(docs)
    store = InMemoryVectorStore()
    store.upsert(
        [VectorRecord(c, [100.0, 0.0] if c.merchant_id == "m_second" else [1.0, 0.0]) for c in docs]
    )
    hybrid = HybridRetriever(
        bm, VectorRetriever(store, Embedder()), rrf_k=60, cache=MemoryRetrievalCache(ttl_seconds=60)
    )
    return docs, bm, store, hybrid


@pytest.mark.asyncio
async def test_every_layer_filters_before_topk_and_rechecks_results():
    docs, bm, store, hybrid = components()
    with policy_case() as context:
        assert all(c.merchant_id != "m_second" for c in bm.search("policy", top_k=1))
        assert all(c.merchant_id != "m_second" for c in store.search([1.0, 0.0], top_k=1))
        assert all(context.allows(c) for c in await hybrid.retrieve("policy", top_k=3))
        ranked = await DocumentReranker(Reranker()).rerank("policy", docs, top_n=1)
        assert len(ranked) == 1 and ranked[0].merchant_id != "m_second"
        grade = Grade()
        loop = SelfRAGLoop(
            hybrid, DocumentReranker(Reranker()), Rewriter(), grade, Settings(max_retrieval_hops=2)
        )
        results = await loop.retrieve("policy", top_k=3)
        assert grade.calls == 2 and all(context.allows(c) for c in results)
    with policy_case(merchant="m_second"):
        assert store.search([1.0, 0.0], top_k=1)[0].chunk_id == "foreign"
        assert any(c.merchant_id == "m_second" for c in await hybrid.retrieve("policy", top_k=3))


@pytest.mark.asyncio
async def test_authorization_failure_cannot_be_hidden_as_backend_failure():
    docs, bm, store, hybrid = components()

    class Poison:
        async def search(self, *args, **kwargs):
            return [docs[0]]

    hybrid._vector = Poison()
    with policy_case(), pytest.raises(ForbiddenError):
        await hybrid.retrieve("policy", top_k=3)


@pytest.mark.asyncio
async def test_missing_scope_fails_before_embedding_or_backend_calls():
    from app.core.exceptions import UnauthorizedError

    _, bm, store, hybrid = components()
    with pytest.raises(UnauthorizedError):
        bm.search("policy", top_k=1)
    with pytest.raises(UnauthorizedError):
        store.search([1.0, 0.0], top_k=1)
    with pytest.raises(UnauthorizedError):
        await hybrid.retrieve("policy", top_k=1)
