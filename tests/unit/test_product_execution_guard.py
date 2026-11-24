import pytest
from pydantic import BaseModel

from app.agents.nodes.executor import executor_node
from app.agents.nodes.generate import generate_node
from app.agents.product_requirements import extract_requirements
from app.config.settings import Settings
from app.core.auth_context import bind_auth_checker
from app.core.data_scope import bind_data_scope, scope_from_identity
from app.core.exceptions import ToolExecutionError
from app.providers.fake import OfflineLLMProvider
from app.schemas.agent import AgentPlan, PlanStep, ToolResult
from app.schemas.auth import AuthIdentity
from app.tools.base import Tool, ToolContext
from app.tools.product_tools import RecommendInput, SearchInput
from app.tools.registry import ToolRegistry


class CandidateTool(Tool):
    name = "recommend_products"
    description = "test"
    input_model = RecommendInput

    def __init__(self, *, fail=False):
        self.calls = []
        self.fail = fail

    async def execute(self, parsed: BaseModel, context: ToolContext):
        self.calls.append(parsed.model_dump())
        if self.fail:
            raise ToolExecutionError("temporary failure")
        return ToolResult(name=self.name, success=True, output={"products": []})


class SearchTool(CandidateTool):
    name = "search_products"
    input_model = SearchInput


def state(query="推荐300元以内黑色42码的鞋"):
    return {
        "user_id": "u",
        "session_id": "s",
        "trace_id": "t",
        "user_query": query,
        "product_requirements": extract_requirements(query),
        "plan": AgentPlan(
            steps=[
                PlanStep(
                    step_id=1,
                    goal="recommend",
                    tool_name="recommend_products",
                    arguments={"query": "鞋", "max_price_cents": 99999, "color": "白色"},
                )
            ]
        ),
    }


@pytest.mark.asyncio
async def test_omissions_and_model_relaxation_cannot_bypass_user_conditions(monkeypatch):
    monkeypatch.setattr("app.agents.nodes.executor.get_db_session", lambda: None)
    registry = ToolRegistry()
    primary = CandidateTool(fail=True)
    fallback = SearchTool()
    registry.register(primary)
    registry.register(fallback)
    async def valid_identity():
        return AuthIdentity("u", "buyer")
    with bind_auth_checker(valid_identity):
        result = await executor_node(state(), registry=registry, settings=Settings())
    assert result["tool_results"][0].success
    for tool in (primary, fallback):
        assert tool.calls[0]["max_price_cents"] == 30000
        assert tool.calls[0]["color"] == "黑色"
        assert tool.calls[0]["size"] == "42"


@pytest.mark.asyncio
async def test_conflict_stops_product_tools_and_asks_for_clarification(monkeypatch):
    monkeypatch.setattr("app.agents.nodes.executor.get_db_session", lambda: None)
    registry = ToolRegistry()
    tool = CandidateTool()
    registry.register(tool)
    original = state("推荐黑色或者白色42码的鞋")
    update = await executor_node(original, registry=registry, settings=Settings())
    assert tool.calls == []
    answer = await generate_node({**original, **update}, OfflineLLMProvider())
    assert "确认" in answer["final_answer"]


@pytest.mark.asyncio
async def test_invalid_results_and_no_match_cannot_be_recommended():
    original = state()
    for products in (
        [],
        [
            {
                "sku": "BAD",
                "name": "超预算鞋",
                "price_cents": 35000,
                "tags": ["color:黑色", "size:42"],
            }
        ],
    ):
        answer = await generate_node(
            {
                **original,
                "tool_results": [
                    ToolResult(
                        name="recommend_products", success=True, output={"products": products}
                    )
                ],
            },
            OfflineLLMProvider(),
        )
        assert "未查到" in answer["final_answer"]
        assert "超预算鞋" not in answer["final_answer"]


@pytest.fixture(autouse=True)
def trusted_buyer_scope():
    with bind_data_scope(scope_from_identity(AuthIdentity("u", "buyer"))):
        yield
