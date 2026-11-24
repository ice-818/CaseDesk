"""Vector stores and dense retrieval over embedded chunks."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from dataclasses import dataclass

from app.providers.base import EmbeddingProvider
from app.retrieval.policy_scope import (
    EMPTY_CORPUS,
    corpus_of,
    current_policy,
    valid_metadata,
    verify_results,
)
from app.schemas.retrieval import RetrievedChunk

logger = logging.getLogger(__name__)


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=False))


def stable_point_id(chunk_id: str) -> str:
    """Qdrant point IDs must be UUID/int. Derive from chunk_id so re-ingest is stable."""
    return str(uuid.UUID(hashlib.md5(chunk_id.encode(), usedforsecurity=False).hexdigest()))


@dataclass
class VectorRecord:
    chunk: RetrievedChunk
    embedding: list[float]


class InMemoryVectorStore:
    """Used in tests and when Qdrant is unavailable."""

    def __init__(self) -> None:
        self._records: list[VectorRecord] = []
        self.corpus_fingerprint = EMPTY_CORPUS

    def upsert(self, records: list[VectorRecord]) -> None:
        self.corpus_fingerprint = corpus_of([record.chunk for record in records])
        existing = {record.chunk.chunk_id: index for index, record in enumerate(self._records)}
        for record in records:
            if record.chunk.chunk_id in existing:
                self._records[existing[record.chunk.chunk_id]] = record
            else:
                self._records.append(record)

    def search(self, query_vector: list[float], *, top_k: int) -> list[RetrievedChunk]:
        context = current_policy(self.corpus_fingerprint)
        scored: list[tuple[float, VectorRecord]] = [
            (cosine(query_vector, record.embedding), record)
            for record in self._records
            if context.allows(record.chunk)
        ]
        scored.sort(key=lambda item: item[0], reverse=True)
        results: list[RetrievedChunk] = []
        for rank, (score, record) in enumerate(scored[:top_k], start=1):
            chunk = record.chunk.model_copy(deep=True)
            chunk.score = float(score)
            chunk.rank = rank
            chunk.source = "vector"
            results.append(chunk)
        return results

    def ping(self) -> bool:
        return True


class QdrantVectorStore:
    def __init__(self, url: str, collection: str, dim: int) -> None:
        self._url = url
        self._collection = collection + "_scope_v1"
        self.corpus_fingerprint = EMPTY_CORPUS
        self._merchants: set[str] = set()
        self._categories: set[str] = set()
        self._fallback = InMemoryVectorStore()
        self._dim = dim
        self._client = None

    def _ensure_client(self):
        if self._client is None:
            from qdrant_client import QdrantClient

            self._client = QdrantClient(url=self._url, timeout=5.0)
        return self._client

    def ensure_collection(self) -> None:
        from qdrant_client.models import Distance, VectorParams

        client = self._ensure_client()
        names = [item.name for item in client.get_collections().collections]
        if self._collection not in names:
            client.create_collection(
                collection_name=self._collection,
                vectors_config=VectorParams(size=self._dim, distance=Distance.COSINE),
            )

    def upsert(self, records: list[VectorRecord]) -> None:
        from qdrant_client.models import PointStruct

        self.corpus_fingerprint = corpus_of([r.chunk for r in records])
        if any(not valid_metadata(r.chunk) for r in records):
            raise ValueError("unscoped vector records cannot be published")
        self._merchants = {r.chunk.merchant_id for r in records if r.chunk.merchant_id}
        self._categories = {
            r.chunk.applicable_category for r in records if r.chunk.applicable_category
        }
        self._fallback.upsert(records)
        client = self._ensure_client()
        self.ensure_collection()
        points = [
            PointStruct(
                id=stable_point_id(record.chunk.chunk_id),
                vector=record.embedding,
                payload={
                    "policy_layer": record.chunk.policy_layer,
                    "merchant_id": record.chunk.merchant_id or "",
                    "applicable_category": record.chunk.applicable_category or "",
                    "policy_scope_version": record.chunk.policy_scope_version,
                    "corpus_fingerprint": record.chunk.corpus_fingerprint,
                    "chunk_id": record.chunk.chunk_id,
                    "document_id": record.chunk.document_id,
                    "title": record.chunk.title,
                    "content": record.chunk.content,
                    "section": record.chunk.section,
                    "chunk_index": record.chunk.chunk_index,
                    "policy_version": record.chunk.policy_version,
                    "source": record.chunk.metadata.get("source", "knowledge"),
                },
            )
            for record in records
        ]
        if points:
            client.upsert(collection_name=self._collection, points=points)

    def ping(self) -> bool:
        try:
            self._ensure_client().get_collections()
            return True
        except Exception:
            return False

    def _filter(self, context):
        from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

        def eq(key, value):
            return FieldCondition(key=key, match=MatchValue(value=value))

        branches = [
            Filter(
                must=[
                    eq("policy_layer", "platform"),
                    eq("merchant_id", ""),
                    eq("applicable_category", ""),
                ]
            )
        ]
        categories = (
            [context.category]
            if context.category
            else (sorted(self._categories) if context.scope.role == "operator" else [])
        )
        if categories:
            branches.append(
                Filter(
                    must=[
                        eq("policy_layer", "category"),
                        eq("merchant_id", ""),
                        FieldCondition(key="applicable_category", match=MatchAny(any=categories)),
                    ]
                )
            )
        merchant = context.target_merchant_id or context.scope.merchant_id
        merchants = (
            [merchant]
            if merchant
            else (sorted(self._merchants) if context.scope.role == "operator" else [])
        )
        if merchants:
            branches.append(
                Filter(
                    must=[
                        eq("policy_layer", "merchant"),
                        FieldCondition(key="merchant_id", match=MatchAny(any=merchants)),
                        FieldCondition(
                            key="applicable_category", match=MatchAny(any=["", *categories])
                        ),
                    ]
                )
            )
        return Filter(
            must=[
                eq("policy_scope_version", 1),
                eq("corpus_fingerprint", context.corpus_fingerprint),
                Filter(should=branches),
            ]
        )

    def search(self, query_vector: list[float], *, top_k: int) -> list[RetrievedChunk]:
        context = current_policy(self.corpus_fingerprint)
        query_filter = self._filter(context)
        try:
            client = self._ensure_client()
            if hasattr(client, "search"):
                hits = client.search(
                    collection_name=self._collection,
                    query_vector=query_vector,
                    query_filter=query_filter,
                    limit=top_k,
                    with_payload=True,
                )
            else:
                hits = client.query_points(
                    collection_name=self._collection,
                    query=query_vector,
                    query_filter=query_filter,
                    limit=top_k,
                    with_payload=True,
                ).points
        except Exception:
            logger.warning("qdrant_search_failed_using_scoped_memory")
            return self._fallback.search(query_vector, top_k=top_k)
        results = []
        for rank, hit in enumerate(hits, start=1):
            payload = hit.payload or {}
            try:
                chunk = RetrievedChunk(
                    chunk_id=payload["chunk_id"],
                    document_id=payload["document_id"],
                    title=payload["title"],
                    content=payload["content"],
                    score=float(hit.score),
                    rank=rank,
                    source="vector",
                    section=payload.get("section"),
                    chunk_index=int(payload.get("chunk_index") or 0),
                    policy_version=payload["policy_version"],
                    policy_layer=payload["policy_layer"],
                    merchant_id=payload["merchant_id"] or None,
                    applicable_category=payload["applicable_category"] or None,
                    policy_scope_version=payload["policy_scope_version"],
                    corpus_fingerprint=payload["corpus_fingerprint"],
                    metadata={"source": str(payload.get("source", "qdrant"))},
                )
            except (KeyError, ValueError, TypeError):
                continue
            verify_results(context, [chunk])
            results.append(chunk)
        return results


class VectorRetriever:
    def __init__(
        self, store: InMemoryVectorStore | QdrantVectorStore, embedder: EmbeddingProvider
    ) -> None:
        self.store = store
        self.embedder = embedder

    async def search(self, query: str, *, top_k: int) -> list[RetrievedChunk]:
        current_policy(self.store.corpus_fingerprint)
        vectors = await self.embedder.embed([query])
        if not vectors:
            return []
        # Qdrant client is sync; do not block the event loop.
        return await asyncio.to_thread(self.store.search, vectors[0], top_k=top_k)

    async def ping(self) -> bool:
        return await asyncio.to_thread(self.store.ping)


def build_records(
    chunks: list[RetrievedChunk],
    embeddings: list[list[float]],
) -> list[VectorRecord]:
    if len(chunks) != len(embeddings):
        raise ValueError("chunk / embedding length mismatch")
    return [
        VectorRecord(chunk=chunk, embedding=embedding)
        for chunk, embedding in zip(chunks, embeddings, strict=False)
    ]
