# ADR 0021: workflow-oriented Chinese product workbench

## Status

Accepted for the connected offline competition demonstration.

## Context

Module checkpoints are useful for implementation and audit, but users approach the product through
a scientific question. The prior M20 page exposed only aggregate counters and contained corrupted
Chinese text, so it did not explain how sources, extracted values, evidence, quality decisions,
and deliverables relate.

## Decision

1. The product navigation is organized around six user-facing stages: research requirements,
   multi-source discovery, parsing/extraction, cleaning/integration, quality validation, and
   delivery. Module identifiers remain in technical artifacts rather than primary navigation.
2. A strict `WorkbenchSnapshot` projects existing immutable results into bounded presentation
   models. It includes sources, raw artifacts and parser routes, raw/normalized/fused fields,
   evidence locations, quality gates, review issues, retrieval scores, graph topology, chart points,
   scientific-format metrics, and delivery availability.
3. Light-curve and evidence-graph canvases render actual M11 and M19 outputs. Tables expose detail
   from the same run rather than decorative placeholders or hard-coded success claims.
4. Formal Gold availability still derives only from M18. The interface visibly explains blocked
   exports and keeps the review package downloadable.

## Consequences

- The offline workbench is a coherent product demonstration while preserving module-level audit
  contracts underneath it.
- Browser acceptance must check Chinese encoding, every navigation view, canvas pixels, responsive
  overflow, interaction, API errors, and downloads.
- The current process-local demo is not a multi-user production deployment; identity, persistence,
  live-source progress streaming, and review write actions remain future work.
