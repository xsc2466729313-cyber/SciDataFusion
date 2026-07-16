# ADR 0029: Risk-tiered AI-assisted review

Status: Accepted for v1.3.0.

## Context

The workbench presented every not-yet-materialized field as pending review. In live discovery most
of those fields are actually waiting for source evidence, not a human decision. Mixing these states
overstates review cost and makes it impossible to see which work can proceed automatically.

## Decision

1. Separate automatic processing, waiting for evidence, and genuine human review in the product
   contract and UI.
2. Deterministic gates remain authoritative. Passing gates need no human approval.
3. Qwen may assess source relevance and propose bounded remediation, but it may not create, repair,
   normalize, select, or mutate a scientific value.
4. Human review is reserved for unresolved semantic conflicts explicitly routed as
   `request_human`. Missing evidence remains blocked or queued for automatic retrieval/reparse.
5. Return a strict review-automation summary with policy version, route counts, AI-use status, and
   deduplicated request/response hashes for search and model calls.
6. A model recommendation is never evidence and cannot pass a scientific quality gate by itself.

## Consequences

- Operators see the actual manual-review workload instead of counting automated evidence work.
- Search, source assessment, and quality-review calls remain tamper-evident without exposing
  prompts, source content, credentials, or scientific values.
- The approach reduces unnecessary human checkpoints while preserving conservative Gold release.
