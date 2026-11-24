from pathlib import Path

import pytest

from app.core.exceptions import ForbiddenError, ResourceNotFoundError, UnauthorizedError
from app.providers.embeddings import HashEmbeddingProvider
from app.retrieval.cleaning import dedupe_chunks
from app.retrieval.ingest import KnowledgeIngestor
from app.retrieval.policy_manifest import LEGACY_DOCUMENTS, corpus_fingerprint, document_scope
from app.retrieval.policy_scope import (
    PolicyContext,
    authorize_policy_target,
    current_policy,
    policy_request,
)
from tests.merchant_support import actor, database
from tests.policy_support import chunk, policy_case


def test_no_scope_and_fake_target_are_rejected():
    with pytest.raises(UnauthorizedError):
        current_policy("test-corpus")
    with policy_case() as context:
        with pytest.raises(ForbiddenError):
            PolicyContext(context.scope, "test-corpus", target="m_second")
        with pytest.raises(ForbiddenError):
            current_policy("other-corpus")


def test_scope_and_category_visibility():
    docs = [
        chunk(),
        chunk("a", "m_demo"),
        chunk("b", "m_second"),
        chunk("cat", category="apparel"),
        chunk("food", category="food"),
    ]
    with policy_case() as context:
        assert [c.chunk_id for c in docs if context.allows(c)] == ["public", "a", "cat"]
    with policy_case("buyer", None) as context:
        assert [c.chunk_id for c in docs if context.allows(c)] == ["public", "cat"]
    with policy_case("operator", None, category=None) as context:
        assert all(context.allows(c) for c in docs)
    assert (
        len(dedupe_chunks([chunk("a", "m_demo"), chunk("b", "m_second"), chunk("a2", "m_demo")]))
        == 2
    )


def test_manifest_requires_registered_ownership_and_real_merchants(tmp_path):
    assert len(LEGACY_DOCUMENTS) == 39
    for name in LEGACY_DOCUMENTS:
        assert (
            document_scope(Path("knowledge") / name, Path("knowledge"), frozenset())["merchant_id"]
            is None
        )
    assert (
        document_scope(tmp_path / "merchants/m_demo/a.md", tmp_path, frozenset({"m_demo"}))[
            "merchant_id"
        ]
        == "m_demo"
    )
    for path in ("unknown.md", "merchants/m_fake/a.md", "misc/a.md"):
        with pytest.raises(ValueError):
            document_scope(tmp_path / path, tmp_path, frozenset({"m_demo"}))
    assert corpus_fingerprint([("a", b"a", {})]) != corpus_fingerprint([("a", b"b", {})])


@pytest.mark.asyncio
async def test_target_comes_only_from_authorized_resources(tmp_path):
    engine, factory = await database(tmp_path / "focus.db")
    try:
        with actor("u_demo", "buyer", None), policy_request():
            async with factory() as db:
                await authorize_policy_target(db, order_id="bu_demo")
                context = current_policy("test-corpus")
                assert context.target_merchant_id == "m_second"
                assert context.allows(chunk("b", "m_second"))
                assert not context.allows(chunk("a", "m_demo"))
                with pytest.raises(ResourceNotFoundError):
                    await authorize_policy_target(db, order_id="aother")
                await authorize_policy_target(db, session_id="platform")
                assert current_policy("test-corpus").target_merchant_id is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_ingest_records_scope_ids_and_changes_fingerprint_without_editing_legacy():
    ingestor = KnowledgeIngestor(
        "knowledge", HashEmbeddingProvider(dim=8), merchants=frozenset({"m_demo", "m_second"})
    )
    chunks, records = await ingestor.load()
    assert len(records) == len(chunks)
    assert {c.merchant_id for c in chunks if c.policy_layer == "merchant"} == {"m_demo", "m_second"}
    assert len({c.corpus_fingerprint for c in chunks}) == 1
    assert all(c.policy_scope_version == 1 and c.policy_version == "unresolved" for c in chunks)


@pytest.mark.asyncio
async def test_recursive_ingest_rejects_unregistered_documents_and_unknown_merchant(tmp_path):
    from app.retrieval.unstructured_parser import list_knowledge_files

    root = tmp_path / "knowledge"
    good = root / "platform" / "public.md"
    good.parent.mkdir(parents=True)
    good.write_text("# Public\nPolicy text", encoding="utf8")
    unknown = root / "merchants" / "not-in-db" / "secret.md"
    unknown.parent.mkdir(parents=True)
    unknown.write_text("private", encoding="utf8")
    hidden = root / ".hidden" / "secret.md"
    hidden.parent.mkdir()
    hidden.write_text("hidden", encoding="utf8")
    assert set(list_knowledge_files(root)) == {good, unknown}
    with pytest.raises(ValueError, match="ownership"):
        await KnowledgeIngestor(
            str(root), HashEmbeddingProvider(dim=8), merchants=frozenset({"m_demo"})
        ).load()


def test_scope_qualified_ids_and_fusion_do_not_merge_different_merchants():
    from app.retrieval.chunking import scoped_chunk_id
    from app.retrieval.fusion import reciprocal_rank_fusion

    first = chunk("same", "m_demo")
    second = chunk("same", "m_second")
    assert scoped_chunk_id(first) != scoped_chunk_id(second)
    with policy_case("operator", None, category=None):
        assert len(reciprocal_rank_fusion([[first], [second]])) == 2


def test_catalog_category_alias_and_policy_version_dedup_boundaries():
    with policy_case(category=chr(0x670D) + chr(0x9970)) as context:
        assert context.category == "apparel"
        assert context.allows(chunk("clothing", category="apparel"))
        with pytest.raises(ForbiddenError):
            PolicyContext(context.scope, "test-corpus", scope_format_version=2)
    first = chunk("a", "m_demo")
    revised = first.model_copy(update={"chunk_id": "revised", "policy_version": "v2"})
    category = first.model_copy(
        update={"chunk_id": "different-category", "applicable_category": "apparel"}
    )
    assert len(dedupe_chunks([first, revised, category])) == 3
