"""Reciprocal Rank Fusion.

RRF(d) = sum_i 1 / (k + rank_i(d))

k=60 is the original Cormack, Clarke, Buettcher default. We use rank rather
than raw scores because BM25 and cosine are not on the same scale.
Duplicate chunk_ids are merged; first-seen metadata wins, scores are fused.
"""

from __future__ import annotations

from app.core.constants import DEFAULT_RRF_K
from app.retrieval.policy_scope import chunk_identity, context_for_chunks, verify_results
from app.schemas.retrieval import RetrievedChunk


def reciprocal_rank_fusion(
    ranked_lists: list[list[RetrievedChunk]],
    *,
    k: int = DEFAULT_RRF_K,
) -> list[RetrievedChunk]:
    if k <= 0:
        raise ValueError("RRF k must be positive")
    chunks = [chunk for ranked in ranked_lists for chunk in ranked]
    verify_results(context_for_chunks(chunks), chunks)
    scores: dict[str, float] = {}
    canonical: dict[str, RetrievedChunk] = {}
    for ranked in ranked_lists:
        for rank_position, chunk in enumerate(ranked, start=1):
            identity = chunk_identity(chunk)
            scores[identity] = scores.get(identity, 0.0) + 1.0 / (k + rank_position)
            if identity not in canonical:
                canonical[identity] = chunk.model_copy(deep=True)
    fused = []
    for chunk_id, score in sorted(scores.items(), key=lambda item: item[1], reverse=True):
        chunk = canonical[chunk_id]
        chunk.score = score
        chunk.source = "rrf"
        fused.append(chunk)
    for index, chunk in enumerate(fused, start=1):
        chunk.rank = index
    return fused
