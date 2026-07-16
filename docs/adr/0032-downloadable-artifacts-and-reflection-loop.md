# ADR 0032: Downloadable artifacts and checkpointed Agent reflection

Status: Accepted for v1.3.0.

## Context

Online acquisition persisted content-addressed bytes, but users could not retrieve those bytes from
the workbench. A single search/download pass also stopped after weak results such as landing pages,
even when the requested research material was still missing.

## Decision

1. Expose a current-topic download endpoint addressed only by a lowercase SHA-256 already present
   in the task's workbench projection. Read through the Bronze store so every response revalidates
   the content hash, and derive the attachment suffix from sniffed media type rather than a remote
   filename.
2. Execute a bounded four-round `search -> acquire -> evaluate -> reflect` loop. Each round records
   inputs, measured gaps, decision, next query, model invocation proof, and a canonical proof hash in
   DuckDB.
3. When a fetched object is an HTML landing page, parse only `href` attributes and immediately
   prioritize at most two same-host HTTPS links whose paths identify machine-readable formats.
   Each attachment is still subject to exact locator approval, DNS pinning, byte budgets, content
   sniffing, and the five-attempt round limit; scripts and cross-host links are never followed.
4. Define the material target as at least three unique artifacts from at least two source domains,
   including one machine-readable table, scientific file, or data archive whose bounded content
   preview passes a separate strict Qwen qualification. The qualification must confirm relevance to
   the research goal, actual scientific records, and confidence of at least 0.7. Web manifests,
   method-only tables, landing pages, and PDF documents remain downloadable evidence but cannot
   satisfy the data target.
5. When the target is not met, the critic may propose only a new natural-language query. Its output
   is strictly validated and cannot invent URLs, files, evidence, or scientific values. Invalid
   reflection falls back to a deterministic gap-specific query.
6. A per-run round limit is a cost and availability boundary, not a success claim. An unmet run is
   checkpointed with its next query and immutable reflection history; Gold remains unavailable.

## Consequences

- Users can download and independently hash every acquired source object.
- The Agent changes evidence routes based on observed failures instead of repeating one query.
- PDF-only acquisition is no longer reported as meeting a data-oriented target.
- A file extension alone cannot pass: semantic qualification decisions and their model proof hashes
  are persisted alongside the reflection checkpoint.
- External authentication, anti-bot controls, and unavailable public data remain explicit blockers;
  they are never bypassed or replaced with invented values.
