# ADR-0004: Capability-backed, fail-closed search planning

## Status

Accepted for the M04 search-planning checkpoint.

## Context

A confirmed data contract says which scientific fields and source types are acceptable, but it does
not prove that a Connector exists or is healthy. Treating a static source list as live health would
produce falsely executable plans. Conversely, putting network calls into planning would mix query
intent, transport policy, credentials, source assessment, and nondeterministic external state in one
checkpoint, weakening replay and making budget behavior difficult to verify.

The planner also needs the research subject, not only output field names. Guessing an entity such as
`Type Ia supernova` from a generic `object_id` field would be scientifically unsafe.

## Decision

1. M03 contracts preserve evidence-grounded `research_concepts` for target entities and variables;
   these concepts participate in the immutable contract hash.
2. M04 loads a strict, size-limited, canonical-hash-verified source-capability registry. It describes
   protocols, adapter operations, domains, source types, costs, and term expansions only.
3. Runtime availability is an independent injected set of source IDs. The default is empty and
   fails closed with an `unsupported` plan and explicit gaps. An offline fixture must select
   `simulated_demo` explicitly; static registry membership never implies health.
4. Planning remains deterministic. Registry terms expand evidence-grounded concepts; normalized
   query hashes suppress duplicates and historical replay while preserving an auditable gap.
5. Every query is bound to a registered source operation, target fields and gates, expected artifact
   types, rationale, result limit, and finite cost/duration estimate. Budget exhaustion defers work
   instead of overspending.
6. The coverage template models contract field/source-preference cells before any candidates exist.
   Observed coverage and source quality are M05/M06 responsibilities.
7. The VizieR family uses the `tap_adql` protocol and emits a `tap_adql_discovery` adapter request.
   M04 neither constructs a final server-specific ADQL statement nor accesses VizieR. Translation,
   allowlisted transport, timeout/retry/rate-limit/cache controls, and execution belong to M05.
8. Search plans and child artifacts are content-addressed and cross-linked to the exact contract,
   routing decision, budget policy, registry, result metrics, and immutable
   `search.plan.created` event. Identical process-local requests share one cached result.
9. Stopping decisions are a pure policy function with explicit resource, coverage, diversity,
   primary-source, and stagnation thresholds, allowing M06 to reuse the same semantics.

## Consequences

- M05 receives an executor-oriented, bounded `SearchPlan` without M04 claiming that any external
  service responded.
- Missing or budget-deferred source families remain visible as typed gaps and can never be mistaken
  for successful coverage.
- Adding a source requires a reviewed registry change and later a conforming M05 Connector; a JSON
  entry alone cannot enable production execution.
- M04 fixture success does not establish the V4 `SearchPlan` executability target of at least 98%.
  A benchmark corpus and real Connector evaluation are still required.
- The credentialed competition-environment Qwen proof remains a separate pending deployment item.
