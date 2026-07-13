# ADR 0019: quality-gated, content-addressed delivery

## Status

Accepted and implemented for the first M20 offline slice.

## Context

M20 must make a useful package even when M18 blocks Formal Gold, while never presenting a review
candidate as a publishable dataset. Delivery artifacts need independent integrity checks,
reproducible packaging, bounded download authorization, and an interactive competition path.

## Decision

1. M20 re-verifies the exact M19 request/result and its complete upstream Bronze lineage before
   creating any artifact.
2. CSV and Parquet are generated only from `FormalGoldDataset`. Their rows and values are replayed
   through Polars and must be exactly equivalent. A missing Formal Gold produces an explicit
   `needs_review` package with no tabular files.
3. Every file is stored by SHA-256. The package contains a canonical manifest, data dictionary,
   field-level provenance, quality report, evidence graph, run metrics, reproduction metadata, and
   a verification notebook. ZIP entry order, timestamps, permissions, and UTF-8 handling are fixed.
4. The workbench exposes reduced summaries rather than scientific values. Downloads require a
   short-lived HMAC ticket bound to filename, content hash, and expiry. The signing key is generated
   in memory and is never written to the repository or package.
5. Result checkpoints contain only strict canonical contracts. Artifact bytes remain in the
   separately verified content-addressed delivery store.

## Consequences

- The current Ia fixture downloads a complete review/reproduction package but correctly receives
  `409 quality_gate_failed` for Gold CSV/Parquet.
- A service restart invalidates existing download tickets by design; clients request a new ticket.
- The first slice uses process-local stores and local demo authorization. Durable object storage,
  identity-provider integration, license-specific redaction, HTML reports, and production signing
  key rotation remain deployment work.
- Notebook validation is deterministic and the acceptance test executes its standard-library hash
  verification cell from an extracted package. Broader scientific analysis notebooks remain future
  work.
