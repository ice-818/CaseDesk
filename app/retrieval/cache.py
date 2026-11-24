"""Short-TTL cache for static policy retrieval only.

Never cache order/user-specific tool results. Keys include trusted policy scope,
target, category, corpus fingerprint, query, top_k and a format version.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time

from app.retrieval.policy_scope import current_policy
from app.schemas.retrieval import RetrievedChunk

logger = logging.getLogger(__name__)


def _looks_like_order_query(query: str) -> bool:
    compact = query.upper().replace(" ", "")
    return "ORD" in compact or "订单" in query


def cache_key(query: str, top_k: int) -> str:
    digest = hashlib.sha256(
        json.dumps([current_policy().cache_identity, query, top_k], ensure_ascii=False).encode()
    ).hexdigest()
    return f"ret:scope:v1:{digest}"


class RetrievalCache:
    async def get(self, query: str, top_k: int) -> list[RetrievedChunk] | None:
        current_policy()
        return None

    async def set(self, query: str, top_k: int, chunks: list[RetrievedChunk]) -> None:
        current_policy()
        return None


class MemoryRetrievalCache(RetrievalCache):
    def __init__(self, *, ttl_seconds: int) -> None:
        self._ttl = ttl_seconds
        self._store: dict[str, tuple[float, list[RetrievedChunk]]] = {}

    async def get(self, query: str, top_k: int) -> list[RetrievedChunk] | None:
        context = current_policy()
        if _looks_like_order_query(query):
            return None
        item = self._store.get(cache_key(query, top_k))
        if item is None:
            return None
        expires_at, chunks = item
        if time.monotonic() > expires_at:
            self._store.pop(cache_key(query, top_k), None)
            return None
        if any(not context.allows(chunk) for chunk in chunks):
            return None
        return [chunk.model_copy(deep=True) for chunk in chunks]

    async def set(self, query: str, top_k: int, chunks: list[RetrievedChunk]) -> None:
        context = current_policy()
        if _looks_like_order_query(query):
            return
        if any(not context.allows(chunk) for chunk in chunks):
            return
        self._store[cache_key(query, top_k)] = (
            time.monotonic() + self._ttl,
            [chunk.model_copy(deep=True) for chunk in chunks],
        )


class RedisRetrievalCache(RetrievalCache):
    def __init__(self, redis, *, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl = ttl_seconds

    async def get(self, query: str, top_k: int) -> list[RetrievedChunk] | None:
        context = current_policy()
        if _looks_like_order_query(query):
            return None
        try:
            raw = await self._redis.get(cache_key(query, top_k))
        except Exception:
            logger.warning("retrieval_cache_redis_get_failed")
            return None
        if not raw:
            return None
        try:
            payload = json.loads(raw)
            chunks = [RetrievedChunk.model_validate(item) for item in payload]
        except (ValueError, TypeError):
            return None
        if any(not context.allows(chunk) for chunk in chunks):
            return None
        return [chunk.model_copy(deep=True) for chunk in chunks]

    async def set(self, query: str, top_k: int, chunks: list[RetrievedChunk]) -> None:
        context = current_policy()
        if _looks_like_order_query(query):
            return
        if any(not context.allows(chunk) for chunk in chunks):
            return
        try:
            await self._redis.set(
                cache_key(query, top_k),
                json.dumps([c.model_dump(mode="json") for c in chunks], ensure_ascii=False),
                ex=self._ttl,
            )
        except Exception:
            logger.warning("retrieval_cache_redis_set_failed")
