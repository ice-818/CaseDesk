"""Reproducible ranking properties and an exhaustive after-sale transition matrix."""

import math
from copy import deepcopy
from itertools import pairwise, product
from random import Random

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.agents.product_requirements import ProductRequirements
from app.core.data_scope import bind_data_scope, scope_from_identity
from app.core.enums import AftersaleStatus
from app.core.exceptions import InvalidRequestError
from app.core.request_scope import request_session
from app.ranking.product import RuleBasedProductRanker
from app.repositories.models import RefundRow, ReturnRow
from app.repositories.order_repository import OrderRepository
from app.schemas.auth import AuthIdentity
from app.schemas.order import Product
from app.services.seed import seed_demo_data
from tests.database_support import migrate_test_database

pytestmark = pytest.mark.property

# Local PRNGs make input generation deterministic across repeated test runs.
SEED = 20261123
BATCHES = 8
SAMPLES_PER_BATCH = 64
TOLERANCE = 1e-12
WEIGHTS = {"relevance": 0.40, "price_fit": 0.25, "inventory": 0.10, "tag_match": 0.25}
EXPECTED = {
    "requested": {"approved", "rejected"},
    "approved": {"completed"},
    "rejected": set(),
    "completed": set(),
}
PREFIXES = {
    "requested": (),
    "approved": ("approved",),
    "rejected": ("rejected",),
    "completed": ("approved", "completed"),
}
ORDER_ID = "ORD10003"
TAGS = ["color:黑色", "color:白色", "size:42", "size:43", "feature:防滑", "feature:缓震"]


# Ranking properties: independent weight oracle, complete output, order and purity.
@pytest.mark.parametrize("batch", range(BATCHES))
def test_ranking_properties(batch):
    rng = Random(SEED + batch)
    ranker = RuleBasedProductRanker()
    for sample in range(SAMPLES_PER_BATCH):
        budget = (None, 0, 30_000, rng.randint(1, 50_000))[sample % 4]
        query = ("", None, "通勤鞋", "耳机", "shoe")[sample % 5]
        requirements = ProductRequirements(
            source_query="", task_query=query,
            constraints={} if budget is None else {"max_price_cents": budget},
            evidence_tags=rng.sample(["防滑", "缓震", "降噪"], rng.randrange(4)),
        )
        ceiling = 30_000 if budget is None else budget
        prices = [0, ceiling + 1, ceiling, ceiling // 2]
        stocks = [0, 1, 10, 11]
        candidates = [
            Product(
                sku=f"SKU-{i:02d}",
                name=rng.choice(["通勤鞋", "耳机", "running shoe"]),
                category=rng.choice(["服饰", "数码", "shoe"]),
                price_cents=prices[i] if i < len(prices) else rng.randint(0, 80_000),
                inventory=stocks[i] if i < len(stocks) else rng.randint(0, 40),
                tags=[TAGS[0], TAGS[2], TAGS[4]] if i == 0 else rng.sample(TAGS, rng.randrange(len(TAGS) + 1)),
            )
            for i in range(rng.randint(4, 12))
        ]
        candidates.append(candidates[0].model_copy(deep=True, update={"sku": "SKU-TIE"}))
        rng.shuffle(candidates)
        label = f"seed={SEED + batch}, sample={sample}"
        before = deepcopy((candidates, requirements))
        ranked = ranker.rank(candidates, requirements)
        assert (candidates, requirements) == before, label
        assert sorted(item.sku for item in ranked) == sorted(item.sku for item in candidates), label
        for item in ranked:
            assert item.feature_scores.keys() == WEIGHTS.keys(), label
            assert all(0 <= score <= 1 for score in item.feature_scores.values()), label
            expected = math.fsum(item.feature_scores[key] * weight for key, weight in WEIGHTS.items())
            assert math.isclose(item.total_score, expected, rel_tol=0, abs_tol=TOLERANCE), label
        for left, right in pairwise(ranked):
            assert left.total_score >= right.total_score, label
            if math.isclose(left.total_score, right.total_score, rel_tol=0, abs_tol=TOLERANCE):
                assert left.sku < right.sku, label
        shuffled = candidates[:]
        rng.shuffle(shuffled)
        shuffled_before = deepcopy(shuffled)
        reordered = ranker.rank(shuffled, requirements)
        assert (candidates, requirements) == before, label
        assert shuffled == shuffled_before, label
        assert sorted(reordered, key=lambda item: item.sku) == sorted(ranked, key=lambda item: item.sku), label


# State-machine properties: drive the public repository over the real migration.
@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    try:
        await migrate_test_database(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory.begin() as db:
            await seed_demo_data(db)
        with bind_data_scope(scope_from_identity(AuthIdentity("u_demo", "buyer"))):
            yield factory
    finally:
        await engine.dispose()


async def create_at(factory, kind, state):
    async with request_session(factory) as db:
        repo = OrderRepository(db)
        create = repo.create_refund if kind == "refund" else repo.create_return
        result = await create(order_id=ORDER_ID, reason="属性测试", trace_id="create")
        await db.commit()
    doc_id = result[f"{kind}_id"]
    for target in PREFIXES[state]:
        await transition(factory, kind, doc_id, target)
    assert (await read_state(factory, kind, doc_id))[0] == state
    return doc_id


async def transition(factory, kind, doc_id, target, *, allowed=True):
    async with request_session(factory) as db:
        repo = OrderRepository(db)
        method = repo.transition_refund if kind == "refund" else repo.transition_return
        kwargs = {f"{kind}_id": doc_id, "target": AftersaleStatus(target), "trace_id": "transition"}
        if allowed:
            assert (await method(**kwargs))["status"] == target
        else:
            with pytest.raises(InvalidRequestError):
                await method(**kwargs)
        # The fresh read below must observe any unintended write from a denied operation.
        await db.commit()


async def read_state(factory, kind, doc_id):
    async with request_session(factory) as db:
        model = RefundRow if kind == "refund" else ReturnRow
        document = await db.get(model, doc_id)
        repo = OrderRepository(db)
        order = await repo.get_order(ORDER_ID)
        refunds = await repo.get_refunds(ORDER_ID)
        return (
            document.status, order.status.value, order.total_cents,
            sorted((r.refund_id, r.status, r.amount_cents) for r in refunds),
        )


def assert_completed(kind, before, after):
    assert after[1] == "refunded"
    assert after[2] == before[2]
    assert len(after[3]) == (len(before[3]) + 1 if kind == "return" else len(before[3]))
    assert [(status, amount) for _, status, amount in after[3]] == [("completed", after[2])]


@pytest.mark.parametrize("kind,source,target", list(product(("refund", "return"), EXPECTED, EXPECTED)))
async def test_aftersale_all_ordered_pairs(session_factory, kind, source, target):
    doc_id = await create_at(session_factory, kind, source)
    before = await read_state(session_factory, kind, doc_id)
    allowed = target in EXPECTED[source]
    await transition(session_factory, kind, doc_id, target, allowed=allowed)
    after = await read_state(session_factory, kind, doc_id)
    if allowed:
        assert after[0] == target
        if target == "completed":
            assert_completed(kind, before, after)
    else:
        assert after == before


@pytest.mark.parametrize("kind", ["refund", "return"])
@pytest.mark.parametrize("source,terminal_path", [
    ("requested", ("rejected",)),
    ("requested", ("approved", "completed")),
    ("approved", ("completed",)),
    ("rejected", ()),
    ("completed", ()),
])
async def test_terminal_state_stays_stable(session_factory, kind, source, terminal_path):
    doc_id = await create_at(session_factory, kind, source)
    for target in terminal_path:
        await transition(session_factory, kind, doc_id, target)
    terminal = await read_state(session_factory, kind, doc_id)
    assert terminal[0] == (terminal_path[-1] if terminal_path else source)
    if terminal[0] == "completed":
        assert terminal[1] == "refunded"
        assert [(status, amount) for _, status, amount in terminal[3]] == [("completed", terminal[2])]
    rng = Random(SEED)
    attempts = list(EXPECTED)
    rng.shuffle(attempts)
    attempts.extend(rng.choices(list(EXPECTED), k=4))
    for target in attempts:
        await transition(session_factory, kind, doc_id, target, allowed=False)
        assert await read_state(session_factory, kind, doc_id) == terminal


# Coverage inventory: 512 generated ranking groups, 32 ordered state pairs, 10 terminal sequences.
