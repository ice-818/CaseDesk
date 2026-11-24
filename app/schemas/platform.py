"""Bounded, typed datasets for two offline evaluation adapters."""

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.order import Product
from app.tools.product_tools import ProductConstraints


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchFixture(StrictModel):
    catalog: list[Product] = Field(min_length=1, max_length=100)
    query: str = Field(default="", max_length=80)
    category: str | None = Field(default=None, max_length=32)
    filters: ProductConstraints = Field(default_factory=ProductConstraints)

    @model_validator(mode="after")
    def unique_skus(self):
        if len({p.sku for p in self.catalog}) != len(self.catalog):
            raise ValueError("duplicate SKU in fixture")
        if any(p.price_cents < 0 or p.inventory < 0 for p in self.catalog):
            raise ValueError("negative catalog price or inventory")
        return self


class SearchCase(StrictModel):
    case_id: str = Field(min_length=1, max_length=64)
    group_id: str = Field(min_length=1, max_length=64)
    input: SearchFixture
    expected: list[str] = Field(max_length=3)

    @model_validator(mode="after")
    def validate_gold(self):
        if len(set(self.expected)) != len(self.expected) or not set(self.expected).issubset(
            {p.sku for p in self.input.catalog}
        ):
            raise ValueError("expected SKU must be unique and present in fixture")
        return self


class UpdateFixture(StrictModel):
    turns: list[str] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def bounded_turns(self):
        if any(not t.strip() or len(t) > 1000 for t in self.turns):
            raise ValueError("turns must contain 1-1000 characters")
        return self


class UpdateExpected(StrictModel):
    constraints: ProductConstraints
    task_query: str | None
    active: bool
    clarification_required: bool
    evidence_tags: list[str] = Field(default_factory=list, max_length=10)


class UpdateCase(StrictModel):
    case_id: str = Field(min_length=1, max_length=64)
    group_id: str = Field(min_length=1, max_length=64)
    input: UpdateFixture
    expected: UpdateExpected


class DatasetCreate(StrictModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    version: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$")
    task: Literal["product_search", "shopping_update"]
    split: Literal["development", "regression", "heldout"] = "regression"
    source_type: Literal["synthetic", "public", "reviewed_feedback"]
    source: str = Field(min_length=1, max_length=2000)
    license: str = Field(min_length=1, max_length=200)
    samples: list[SearchCase | UpdateCase] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def validate_samples(self):
        sample_type = SearchCase if self.task == "product_search" else UpdateCase
        if any(not isinstance(sample, sample_type) for sample in self.samples):
            raise ValueError("sample type does not match dataset task")
        if len({sample.case_id for sample in self.samples}) != len(self.samples):
            raise ValueError("duplicate case_id")
        normalized = [
            json.dumps(sample.input.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
            for sample in self.samples
        ]
        if len(set(normalized)) != len(normalized):
            raise ValueError("duplicate sample input")
        return self


class DatasetReview(StrictModel):
    decision: Literal["approved", "rejected"]
    reviewer: Any = Field(default=None, json_schema_extra={"deprecated": True})
    note: str = Field(min_length=1, max_length=2000)


class EvaluationCreate(StrictModel):
    dataset_id: str = Field(min_length=1, max_length=64)
    candidate_label: str = Field(min_length=1, max_length=80)
