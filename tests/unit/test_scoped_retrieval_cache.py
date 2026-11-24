import pytest

from app.retrieval.cache import MemoryRetrievalCache, RedisRetrievalCache, cache_key
from tests.policy_support import chunk, policy_case


class Redis:
    def __init__(self):
        self.data = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, **kwargs):
        self.data[key] = value


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "redis"])
async def test_cache_scopes_versions_deepcopy_and_poisoning(backend):
    cache = (
        MemoryRetrievalCache(ttl_seconds=60)
        if backend == "memory"
        else RedisRetrievalCache(Redis(), ttl_seconds=60)
    )
    with policy_case():
        first = chunk("a", "m_demo")
        await cache.set("same", 3, [first])
        key = cache_key("same", 3)
        assert key.startswith("ret:scope:v1:")
        first.content = "mutated"
        hit = await cache.get("same", 3)
        assert hit[0].content != "mutated"
        hit[0].content = "another mutation"
        assert (await cache.get("same", 3))[0].content != "another mutation"
    with policy_case(merchant="m_second"):
        assert await cache.get("same", 3) is None
        assert cache_key("same", 3) != key
        await cache.set("same", 3, [chunk("b", "m_second")])
    with policy_case(fingerprint="new-corpus"):
        assert await cache.get("same", 3) is None
    with policy_case():
        assert (await cache.get("same", 3))[0].chunk_id == "a"
        await cache.set("poison", 3, [chunk("foreign", "m_second")])
        assert await cache.get("poison", 3) is None
        if backend == "memory":
            cache._store[key] = (float("inf"), [chunk("foreign", "m_second")])
        else:
            import json

            cache._redis.data[key] = json.dumps(
                [chunk("foreign", "m_second").model_dump(mode="json")]
            )
        assert await cache.get("same", 3) is None


@pytest.mark.asyncio
async def test_same_buyer_and_query_separate_authorized_target_merchants(tmp_path):
    from app.retrieval.policy_scope import (
        authorize_policy_target,
        bind_policy_context,
        current_policy,
        policy_request,
    )
    from tests.merchant_support import actor, database

    engine, factory = await database(tmp_path / "target-cache.db")
    cache = MemoryRetrievalCache(ttl_seconds=60)
    try:
        with actor("u_demo", "buyer", None), policy_request():
            async with factory() as db:
                await authorize_policy_target(db, order_id="au_demo")
                with bind_policy_context(current_policy("test-corpus")):
                    await cache.set("same", 3, [chunk("a", "m_demo")])
                    first = cache_key("same", 3)
                await authorize_policy_target(db, order_id="bu_demo")
                with bind_policy_context(current_policy("test-corpus")):
                    assert cache_key("same", 3) != first
                    assert await cache.get("same", 3) is None
    finally:
        await engine.dispose()
