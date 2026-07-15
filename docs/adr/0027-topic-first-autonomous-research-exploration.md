# ADR 0027: Topic-first autonomous research exploration

Status: Accepted for v1.2.0.

## Context

The workbench required both a research goal and a manually written evidence query. Its online
planner only expanded that query, while the rest of the page continued to project the built-in
Type Ia supernova demonstration. For a different research topic this was confusing and could make
reference data look like topic-specific output.

## Decision

1. Make `research_goal` the only required user input. Keep an optional advanced query override.
2. Require every model-generated `SearchQueryPlan` to include a strict `ResearchExplorationProfile`
   with a topic title, summary, evidence priorities, source types, candidate fields, quality checks,
   target outputs, and visualization direction.
3. Validate the complete model response with Pydantic `extra="forbid"`; reject duplicate plan
   entries and fall back to a deterministic topic-derived plan if validation fails.
4. Let a valid Qwen plan supply all search expressions when the user did not provide an override.
5. Project online runs as `live_discovery`: show only real search sources, planned candidate fields,
   pending quality checks, and a topic-specific interactive graph. Do not expose the built-in
   scientific values, artifacts, quality gates, or delivery package as current-topic results.
6. Keep the existing content-addressed supernova workflow as `reference_demo` for offline,
   reproducible product verification.
7. Do not create scientific values from the exploration profile. It is planning metadata only;
   formal data remains gated by field-level evidence and deterministic parsing.

## Consequences

- A broad research direction is enough to start useful multi-source exploration.
- The page adapts its labels, metrics, candidate fields, quality plan, coverage chart, and 3D graph
  to the current topic.
- Online discovery can finish before topic files are downloaded and parsed. During that state the
  UI intentionally disables CSV and reproduction-package downloads.
- The offline reference workflow remains deterministic and testable without API keys.
