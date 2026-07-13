"""Local BM25 retrieval with task metadata filtering and graph expansion."""

from __future__ import annotations

import re
from dataclasses import dataclass

from rank_bm25 import BM25Okapi  # type: ignore[import-untyped]

from scidatafusion.contracts.knowledge import IndexDocument, KnowledgePolicy

_WORD_PATTERN = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """Tokenize bounded metadata text without interpreting scientific values."""

    return _WORD_PATTERN.findall(text.casefold().replace("_", " "))


@dataclass(frozen=True, slots=True)
class ScoredDocument:
    document: IndexDocument
    sparse_score: float
    graph_score: float
    final_score: float
    graph_path_node_ids: tuple[str, ...]


def retrieve(
    *,
    query: str,
    documents: tuple[IndexDocument, ...],
    adjacency: dict[str, set[str]],
    policy: KnowledgePolicy,
    query_task_id: str,
    permission_tags: tuple[str, ...],
) -> tuple[ScoredDocument, ...]:
    """Filter by task permissions, score with BM25, then apply one-hop graph expansion."""

    allowed = tuple(
        item
        for item in documents
        if item.task_id == query_task_id and set(item.permission_tags) <= set(permission_tags)
    )
    if not allowed:
        return ()
    corpus = [tokenize(item.text) for item in allowed]
    query_tokens = tokenize(query)
    raw_scores = BM25Okapi(corpus).get_scores(query_tokens)
    positive = [max(0.0, float(item)) for item in raw_scores]
    maximum = max(positive, default=0.0)
    sparse = [item / maximum if maximum else 0.0 for item in positive]
    matched_nodes = {
        item.graph_node_id
        for item, tokens in zip(allowed, corpus, strict=True)
        if set(tokens) & set(query_tokens)
    }
    expanded_nodes = set(matched_nodes)
    for node in matched_nodes:
        expanded_nodes.update(adjacency.get(node, set()))
    scored: list[ScoredDocument] = []
    for document, sparse_score in zip(allowed, sparse, strict=True):
        graph_score = 1.0 if document.graph_node_id in expanded_nodes else 0.0
        final_score = policy.sparse_weight * sparse_score + policy.graph_weight * graph_score
        if final_score <= 0.0:
            continue
        path = (document.graph_node_id,) if graph_score else ()
        scored.append(
            ScoredDocument(
                document=document,
                sparse_score=sparse_score,
                graph_score=graph_score,
                final_score=final_score,
                graph_path_node_ids=path,
            )
        )
    return tuple(
        sorted(scored, key=lambda item: (-item.final_score, item.document.document_id))[
            : policy.max_hits
        ]
    )
