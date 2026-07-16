# ADR 0028: Multi-channel Scholar and arXiv discovery

Status: Accepted for v1.3.0.

## Context

Topic-first exploration could generate several queries, but they all used one configured SerpApi
engine. Broad Google results could fill the retained-source limit before scholarly papers or
preprints were represented.

## Decision

1. Add a required channel to every planned query: `google_web`, `google_scholar`, or `arxiv`.
2. Use SerpApi for both Google channels and the public arXiv Atom API for arXiv. No arXiv key is
   required.
3. Require LLM plans with at least three queries to cover all channels. A deterministic fallback
   issues one query per channel.
4. Apply exact endpoint allowlists, timeout, retry, bounded concurrency, cache, and immutable
   request/response hashes to both providers. Space arXiv requests by at least three seconds and
   parse its bounded Atom response with `defusedxml`.
5. Merge one result from each query in rotation before taking the next result, deduplicate by URL,
   and retain at most 20 sources.
6. Keep titles, abstracts, URLs, and model plans as untrusted input. Search and planning may not
   create or mutate scientific values.

## Consequences

- A broad topic yields a more balanced mix of repositories, published papers, and preprints.
- Every result and query visibly identifies its channel and provider proof.
- Google Scholar still requires the user's SerpApi key; arXiv adds no configuration burden.
- arXiv throttling can make that channel slower, and a partial failure remains visible instead of
  blocking successful channels.
