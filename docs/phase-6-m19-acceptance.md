# M19 acceptance: sparse retrieval, evidence graph, and memory quarantine

## Status

Accepted on 2026-07-13 for the first task-private offline slice. This is not acceptance of dense
embedding, Qwen reranking, cross-task retrieval, DocumentIR/domain-corpus indexing, graph community
summaries, approved long-term memory, or benchmark-backed retrieval and graph accuracy.

## Definition of Done

- [x] Strict frozen Pydantic v2 contracts with `extra="forbid"` cover request, policy, runtime,
  index documents/manifest, graph nodes/edges/decisions, retrieval hits/result, task memory,
  metrics, result, and completion event.
- [x] M19 re-verifies the exact M13-M18 request/result/event chain, contract, nested hashes,
  EvidenceAtom references, and Bronze bytes before execution or replay.
- [x] BM25 retrieval uses the maintained `rank-bm25` library after task and permission filtering.
- [x] Every retrieval hit contains source identity, source location, index version, sparse score,
  graph score, final score, and graph-path nodes.
- [x] Graph identities are content-addressed, edges cannot dangle, and evidence references are
  retained.
- [x] The graph participates in three actual recorded decisions: evidence-lineage validation,
  retrieval-context expansion, and memory admission.
- [x] Task memory has deterministic approved/quarantined admission rules and immutable revocation
  that preserves the superseded memory hash.
- [x] Unreviewed M18 output is quarantined and cannot be reused across tasks; knowledge-pollution
  count remains zero.
- [x] Complete results are immutable, checkpointable, single-flight, cancellation isolated,
  tamper evident, and emit one causally linked `knowledge.updated` event.
- [x] No dense embedding, model rerank, cross-task retrieval, network access, scientific-value
  mutation, or cost occurs.
- [x] Architecture, limitations, metrics, verification commands, and offline evidence are recorded.
- [x] Final repository gates and measured totals are recorded after the complete M19 run.

## Offline Ia evidence

`phase6-knowledge-demo` executes the packaged M00-M19 chain without external services. It creates
ten task-private index documents: four EvidenceAtom projections, three quality-gate documents, and
three quality-issue documents. The evidence graph contains eighteen nodes, thirty-three edges, and
three recorded graph decisions. The demo query retrieves all ten documents with traceable source,
location, version, sparse, graph, and final-score metadata. M18 has no formal Gold, so the single
task-memory entry is quarantined and non-reusable.

This is an honest functional proof of traceability, permission filtering, graph participation, and
memory isolation. It cannot establish Recall@20, nDCG@10 improvement, graph-triple precision,
retrieval P95, or cross-domain generalization. Those metrics need versioned judged corpora.

The CLI contains only aggregate counts, artifact hashes, document kinds, graph decision classes,
and memory states. It omits the query, source identities, locations, graph labels, scientific
values, evidence content, goal text, and reviewer identity.

## Verification

```powershell
uv run pytest tests/test_knowledge_service.py tests/test_cli.py -q --no-cov
uv run scidatafusion phase6-knowledge-demo `
  --goal "Study Type Ia supernova light curves using multi-source data integration into CSV." `
  --query "quality evidence observation time magnitude" `
  --confirmed-by "m19-acceptance"
powershell -ExecutionPolicy Bypass -File scripts/check.ps1
```

The final repository gate completed with 617 passing tests and 90.10% total coverage. Ruff and
format checks passed across 205 files, mypy passed across 204 source files, Bandit reported no
issues, the secret scan passed across 676 files, and the dependency audit reported no known
vulnerabilities.

## Known boundaries

- The index contains metadata projections, not raw DocumentIR blocks or a representative knowledge
  corpus. It is task-local and cannot support broad scientific literature questions.
- Dense vector indexing and Qwen reranking are disabled rather than silently replaced. Future
  adapters require allowlist, timeout, retry, rate limit, cache, strict validation, and Mock tests.
- Graph expansion is one hop and does not perform entity extraction, community detection, or
  probabilistic relation inference.
- The current memory is quarantined because M18 is review-blocked. Approval requires quality-passed
  formal Gold; reviewer authentication and durable publication remain deferred.
- Retrieval metrics in this fixture are operational counts, not benchmark quality claims.
