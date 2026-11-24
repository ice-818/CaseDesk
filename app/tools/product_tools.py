"""Product tools: search, detail, recommendation and inventory."""

import re

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.constants import SHOPPING_RECALL_PAGE_SIZES
from app.core.enums import ToolRisk
from app.core.exceptions import ResourceNotFoundError
from app.retrieval.policy_scope import authorize_policy_target
from app.schemas.agent import ToolResult
from app.tools.base import Tool, ToolContext
from app.tools.deps import RepositoryBackedTool


class ProductConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_price_cents: int | None = Field(
        default=None, ge=0, strict=True, description="最低价格，单位分，含边界"
    )
    max_price_cents: int | None = Field(
        default=None, ge=0, strict=True, description="最高价格，单位分，含边界；300元填30000"
    )
    color: str | None = Field(
        default=None,
        min_length=1,
        max_length=32,
        pattern=r"^[^,\s]+$",
        description="明确颜色，如黑色；缺少该规格的商品不得匹配",
    )
    size: str | None = Field(
        default=None,
        min_length=1,
        max_length=32,
        pattern=r"^[^,\s]+$",
        description="明确尺码，如42；不得更换尺码",
    )
    colors: list[str] | None = Field(
        default=None,
        min_length=1,
        max_length=12,
        description="可接受颜色的列表，任一匹配；不能与color同时传入",
    )

    @model_validator(mode="after")
    def validate_price_range(self):
        if self.color is not None and self.colors is not None:
            raise ValueError("use either color or colors")
        if self.colors and any(not value or re.search(r"[,\s]", value) for value in self.colors):
            raise ValueError("invalid color")
        if (
            self.min_price_cents is not None
            and self.max_price_cents is not None
            and self.min_price_cents > self.max_price_cents
        ):
            raise ValueError("minimum price exceeds maximum price")
        return self


class SearchInput(ProductConstraints):
    offset: int = Field(default=0, ge=0)
    query: str = Field(
        min_length=1,
        max_length=80,
        description="商品关键词，如「降噪耳机」；不要填 sku 或订单号",
    )


class SkuInput(BaseModel):
    sku: str = Field(
        min_length=2,
        max_length=32,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="商品 SKU，如 SKU-A1；不要填商品中文名或 order_id",
    )


class RecommendInput(ProductConstraints):
    offset: int = Field(default=0, ge=0)
    category: str | None = Field(
        default=None,
        max_length=32,
        description="可选品类，如「数码」；不要填 sku",
    )
    query: str = Field(
        default="",
        max_length=80,
        description="可选需求描述，如「送女朋友的礼物」；可与 category 二选一",
    )


class SearchProductsTool(RepositoryBackedTool, Tool):
    name = "search_products"
    description = (
        "按关键词搜索商品目录，最多返回约 5 条匹配商品。"
        "何时用：用户给出具体商品名、品类词或属性词要找货，如「无线耳机」「跑步鞋」。"
        "何时不用：已知 sku 用 get_product_detail；「推荐/不知道买什么」用 recommend_products；政策问题用 search_knowledge。"
        "优先使用 search_products 当有明确关键词；模糊需求或推荐语气则用 recommend_products。"
        "参数 query：1–80 字搜索词，不是 sku。例 query=「降噪耳机」，不要填 SKU-A1 或 ORD1001。"
        "硬条件参数 min_price_cents/max_price_cents（整数分，含边界）、color（如黑色）、size（如42），必须完整传递用户明确条件，禁止放宽。"
        "query只填商品关键词，不要把预算和尺码混进搜索词。无匹配时products为空，不得推荐其他规格。"
        "返回 products[]：sku、name、category、price_cents（分）、inventory；后续详情用返回的 sku 调 get_product_detail。"
    )
    risk = ToolRisk.READ
    input_model = SearchInput

    async def execute(self, parsed: SearchInput, context: ToolContext) -> ToolResult:
        products = await self.repository.find_matching_products(
            **parsed.model_dump(), limit=SHOPPING_RECALL_PAGE_SIZES[self.name]
        )
        return ToolResult(
            name=self.name,
            success=True,
            output={"products": [p.model_dump() for p in products]},
        )


class GetProductDetailTool(RepositoryBackedTool, Tool):
    name = "get_product_detail"
    description = (
        "按 SKU 查询单品详情：名称、品类、价格、库存、标签、描述。"
        "何时用：已知 sku，问规格、价格、介绍、这件是什么。"
        "何时不用：还不知道 sku 时先 search_products；只要库存数量用 check_inventory；保修条款用 get_warranty。"
        "优先：仅问有没有货 → check_inventory；还要价格/规格/介绍 → 本工具（已含 inventory）。"
        "参数 sku：2–32 位字母数字_-，例 SKU-A1；不要把商品中文名或 order_id 当 sku。"
        "返回 sku、name、category、price_cents（分）、inventory、tags、description。"
    )
    risk = ToolRisk.READ
    input_model = SkuInput

    async def execute(self, parsed: SkuInput, context: ToolContext) -> ToolResult:
        product = await self.repository.get_product(parsed.sku)
        if product is None:
            raise ResourceNotFoundError()
        await authorize_policy_target(self.repository._session, sku=parsed.sku)
        return ToolResult(name=self.name, success=True, output=product.model_dump())


class RecommendProductsTool(RepositoryBackedTool, Tool):
    name = "recommend_products"
    description = (
        "按品类或需求推荐最多 3 个商品，适合不确定买什么的场景。"
        "何时用：用户说推荐、随便看看、按场景/预算选、不知道买哪款。"
        "何时不用：已有明确搜索词用 search_products；已知 sku 用 get_product_detail。"
        "优先使用 search_products 当用户给出具体商品关键词；否则用本工具。"
        "硬条件参数 min_price_cents/max_price_cents（整数分，含边界）、color（如黑色）、size（如42），必须完整传递用户明确条件，禁止放宽。"
        "query只填商品关键词；category与query同时提供时都必须匹配。无匹配时不得改预算或换尺码。"
        "参数 category 可选品类如「数码」；query 可选商品关键词。都空则在授权目录内按硬条件召回，不附加关键词。不要填 sku。"
        "返回 products 最多 3 条，字段同 search_products；不是个性化实时推荐。"
    )
    risk = ToolRisk.READ
    input_model = RecommendInput

    async def execute(self, parsed: RecommendInput, context: ToolContext) -> ToolResult:
        products = await self.repository.find_matching_products(
            **parsed.model_dump(), limit=SHOPPING_RECALL_PAGE_SIZES[self.name]
        )
        return ToolResult(
            name=self.name,
            success=True,
            output={
                "products": [p.model_dump() for p in products],
                "status": "matched" if products else "no_match",
                "applied_constraints": parsed.model_dump(exclude_none=True, exclude={"offset"}),
                "message": "仅返回符合已传入硬条件且有库存的商品。"
                if products
                else "当前商品目录未找到符合全部已传入条件且有库存的商品；不得擅自放宽条件。",
            },
        )


class CheckInventoryTool(RepositoryBackedTool, Tool):
    name = "check_inventory"
    description = (
        "按 SKU 只查询库存数量，不含价格与规格。"
        "何时用：已知 sku，只问有货吗、库存多少。"
        "何时不用：还要价格/规格/介绍用 get_product_detail；不知 sku 先 search_products。"
        "优先：仅库存 → 本工具；同时要价格或详情 → get_product_detail（无需再调本工具）。"
        "参数 sku：例 SKU-A1，不是商品中文名。"
        "返回 {sku, inventory}；0 表示无货，不要编造补货日期。"
    )
    risk = ToolRisk.READ
    input_model = SkuInput

    async def execute(self, parsed: SkuInput, context: ToolContext) -> ToolResult:
        product = await self.repository.get_product(parsed.sku)
        if product is None:
            raise ResourceNotFoundError()
        return ToolResult(
            name=self.name,
            success=True,
            output={"sku": product.sku, "inventory": product.inventory},
        )
