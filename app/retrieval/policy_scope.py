"""Policy visibility requires an authenticated data scope and validated resource focus."""

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from app.core.data_scope import DataScope, require_data_scope
from app.core.exceptions import ForbiddenError
from app.schemas.retrieval import RetrievedChunk

FORMAT_VERSION = 1
_SEAL = object()
EMPTY_CORPUS = hashlib.sha256(b"empty-policy-corpus").hexdigest()


@dataclass(frozen=True)
class _Target:
    merchant_id: str
    category: str | None
    scope_key: str
    proof: object


_focus: ContextVar[_Target | None] = ContextVar("policy_focus", default=None)
_context: ContextVar["PolicyContext | None"] = ContextVar("policy_context", default=None)


@dataclass(frozen=True)
class PolicyContext:
    scope: DataScope
    corpus_fingerprint: str
    target: _Target | None = None
    category: str | None = None
    scope_format_version: int = FORMAT_VERSION

    def __post_init__(self):
        aliases = {
            "\u670d\u9970": "apparel",
            "\u6570\u7801": "electronics",
            "\u98df\u54c1": "food",
            "\u73e0\u5b9d": "jewelry",
            "\u7535\u6c60": "battery",
        }
        object.__setattr__(self, "category", aliases.get(self.category, self.category))
        self.validate()

    @property
    def target_merchant_id(self):
        return self.target.merchant_id if self.target else None

    def validate(self):
        self.scope.assert_current()
        if not self.corpus_fingerprint or self.scope_format_version != FORMAT_VERSION:
            raise ForbiddenError("invalid policy scope version")
        if self.target is not None and (
            not isinstance(self.target, _Target)
            or self.target.proof is not _SEAL
            or self.target.scope_key != self.scope.scope_key
        ):
            raise ForbiddenError("policy target must originate from an authorized resource")
        if (
            self.scope.mode == "shop"
            and self.target
            and self.target.merchant_id != self.scope.merchant_id
        ):
            raise ForbiddenError()

    @property
    def cache_identity(self):
        self.validate()
        return [
            self.scope.scope_key,
            self.target_merchant_id,
            self.category,
            self.corpus_fingerprint,
            self.scope_format_version,
        ]

    def allows(self, chunk: RetrievedChunk) -> bool:
        self.validate()
        if not valid_metadata(chunk) or chunk.corpus_fingerprint != self.corpus_fingerprint:
            return False
        if (
            chunk.applicable_category
            and chunk.applicable_category != self.category
            and not (self.scope.role == "operator" and self.category is None)
        ):
            return False
        if chunk.policy_layer == "merchant":
            allowed = self.target_merchant_id or self.scope.merchant_id
            return chunk.merchant_id == allowed if allowed else self.scope.role == "operator"
        return True


def valid_metadata(chunk: RetrievedChunk) -> bool:
    if (
        chunk.policy_scope_version != FORMAT_VERSION
        or not chunk.corpus_fingerprint
        or not chunk.policy_version
    ):
        return False
    if chunk.policy_layer == "platform":
        return chunk.merchant_id is None and chunk.applicable_category is None
    if chunk.policy_layer == "category":
        return chunk.merchant_id is None and bool(chunk.applicable_category)
    return chunk.policy_layer == "merchant" and bool(chunk.merchant_id)


def policy_identity(chunk: RetrievedChunk):
    return (
        chunk.policy_layer,
        chunk.merchant_id,
        chunk.applicable_category,
        chunk.policy_version,
        chunk.policy_scope_version,
    )


def chunk_identity(chunk: RetrievedChunk):
    return json.dumps(
        [*policy_identity(chunk), chunk.chunk_id], ensure_ascii=False, separators=(",", ":")
    )


def current_policy(corpus_fingerprint: str | None = None) -> PolicyContext:
    scope = require_data_scope()
    existing = _context.get()
    if existing is not None:
        existing.validate()
        if corpus_fingerprint is not None and existing.corpus_fingerprint != corpus_fingerprint:
            raise ForbiddenError("policy corpus changed")
        return existing
    if corpus_fingerprint is None:
        raise ForbiddenError("policy context required")
    target = _focus.get()
    return PolicyContext(scope, corpus_fingerprint, target, target.category if target else None)


def corpus_of(chunks: list[RetrievedChunk]) -> str:
    values = {c.corpus_fingerprint for c in chunks}
    if not values:
        return EMPTY_CORPUS
    if len(values) != 1 or not next(iter(values)):
        raise ForbiddenError("invalid or mixed policy corpus")
    return next(iter(values))


def verify_results(context: PolicyContext, chunks: list[RetrievedChunk]) -> None:
    context.validate()
    if any(not context.allows(c) for c in chunks):
        raise ForbiddenError("retriever returned evidence outside policy scope")


@contextmanager
def bind_policy_context(context: PolicyContext) -> Iterator[None]:
    context.validate()
    token = _context.set(context)
    try:
        yield
    finally:
        _context.reset(token)


@contextmanager
def policy_request():
    focus = _focus.set(None)
    context = _context.set(None)
    try:
        yield
    finally:
        _context.reset(context)
        _focus.reset(focus)


async def authorize_policy_target(db, *, order_id=None, sku=None, session_id=None):
    from app.core.exceptions import ResourceNotFoundError
    from app.repositories.order_repository import OrderRepository
    from app.repositories.session_repository import SessionRepository

    scope = require_data_scope()
    if sum(value is not None for value in (order_id, sku, session_id)) != 1:
        raise ForbiddenError("exactly one policy resource is required")
    category = None
    if order_id is not None:
        row = await OrderRepository(db).get_visible_order(order_id)
        merchant = row.merchant_id
    elif sku is not None:
        product = await OrderRepository(db).get_product(sku)
        if product is None:
            raise ResourceNotFoundError()
        merchant = product.merchant_id
        category = product.category
    else:
        session = await SessionRepository(db).authorized(session_id)
        if session.channel != "shop":
            _focus.set(None)
            return
        merchant = session.merchant_id
    if not merchant:
        raise ForbiddenError()
    target = _Target(merchant, category, scope.scope_key, _SEAL)
    _focus.set(target)


def context_for_chunks(chunks):
    return current_policy() if _context.get() is not None else current_policy(corpus_of(chunks))


def policy_operation(function):
    from functools import wraps

    @wraps(function)
    async def wrapped(self, *args, **kwargs):
        context = current_policy(getattr(self, "corpus_fingerprint", None))
        with bind_policy_context(context):
            return await function(self, *args, **kwargs)

    return wrapped
