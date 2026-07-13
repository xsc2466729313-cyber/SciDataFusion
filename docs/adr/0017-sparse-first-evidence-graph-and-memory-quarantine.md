# ADR 0017: sparse-first evidence graph and memory quarantine

## Status

Accepted and implemented for the first M19 offline slice.

## Context

M19 must provide traceable retrieval and reusable task knowledge without allowing unreviewed
scientific conclusions to contaminate later tasks. The current M18 result is review-blocked and has
no formal Gold dataset, but it contains verified EvidenceAtoms, quality gates, and quality issues
that are useful for task-local retrieval and diagnosis.

The full specification calls for sparse and dense retrieval, graph context, Qwen reranking, domain
documents, and long-term task memory. Enabling model-backed channels without a judged corpus and an
approved memory entry would overstate retrieval quality and risk knowledge pollution.

## Decision

1. M19 re-verifies the complete M13-M18 lineage and Bronze evidence before execution or checkpoint
   replay.
2. The first index contains deterministic metadata projections of M13 EvidenceAtoms, M18 quality
   gates, and M18 quality issues. It never indexes raw scientific values or untrusted document
   instructions.
3. `rank-bm25` implements the sparse channel. Task identity and permission tags filter documents
   before scoring. Dense embedding and model reranking are explicit disabled capabilities.
4. Every retrieval hit stores source identity, location, index version, normalized sparse score,
   graph score, final weighted score, and graph path nodes.
5. The evidence graph links task, evidence, fields, gates, issues, and memory with content-addressed
   nodes and edges. It must participate in three recorded decisions: lineage validation, retrieval
   expansion, and memory admission.
6. Task memory is approved only when M18 has passed quality gates and produced formal Gold.
   Otherwise it is quarantined, non-reusable, and carries structured reasons.
7. Revocation creates a new immutable memory entry that references the prior memory hash; it never
   mutates or deletes historical memory.
8. All artifacts are checkpointable and causally linked through `knowledge.updated` without model
   or network execution.

## Consequences

- The current fixture yields useful task-local retrieval while reporting one quarantined memory and
  zero knowledge-pollution events.
- Sparse retrieval and graph expansion are reproducible offline, but they do not establish
  Recall@20, nDCG improvement, graph precision, or latency targets.
- Raw DocumentIR blocks, external domain documents, vectors, Qwen reranking, graph communities, and
  cross-task permissions require later accepted slices.
- Quarantined memory can support same-task inspection but cannot influence another task.
- Process-local checkpoints prove canonical replay and conflict rejection, not durable distributed
  index persistence.
