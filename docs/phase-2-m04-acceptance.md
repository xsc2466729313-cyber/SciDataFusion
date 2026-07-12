# M04 acceptance: capability-backed search strategy and coverage planning

## Exit criteria

- M04 accepts only an integrity-valid, explicitly confirmed `ScientificDataContract`, its exact
  succeeded/formal M02 route, and the matching finite M00 budget policy. Cross-run, cross-task,
  draft, and tampered inputs fail before planning.
- M03 preserves evidence-referenced research entities and variables as `research_concepts`, so
  search terms remain traceable to the accepted research goal instead of being inferred from field
  names or invented by the planner.
- A strict, size-limited, canonical-hash-verified registry declares source category, protocol,
  operation dialect, domain, supported contract source types, estimated query cost/duration, and
  versioned multilingual term expansions.
- Registry declarations do not represent Connector availability or health. `SearchPlanner`
  defaults to zero runtime source IDs and returns a structured `unsupported` plan with explicit
  capability gaps. Fixture capabilities require an explicit `simulated_demo` request mode.
- The Ia plan covers the applicable literature metadata, data repository, astronomy domain
  database, and supplement/web query families. Every active query names its target contract
  fields, quality gates, expected artifact types, source operation, budget estimate, and rationale.
- Query expansion is deterministic and registry-grounded. Unicode NFKC/case/whitespace
  normalization removes duplicate and replayed queries without silently hiding the resulting gap.
- The initial coverage matrix is the contract-field/source-preference product and binds planned
  queries to field and quality-gate targets. At M04, observed candidate counts remain zero.
- Search-budget allocation respects remaining cost, duration, and round limits, defers queries
  rather than overspending, and records each deferral as a visible gap.
- The pure stopping policy has deterministic precedence for cancellation, cost, duration,
  download, model-token, round, coverage, primary-source, and stagnation conditions.
- `SearchPlan`, child artifacts, result metrics, and `search.plan.created` are strict linked
  contracts. Semantic hashes, stable IDs, idempotency keys, and process-local replay reject
  mutation and return one immutable result for concurrent identical requests.

## Ia adapter boundary

M04 performs no network I/O. For VizieR it emits a protocol-tagged `tap_adql_discovery` adapter
request with bounded parameters and contract-field intent. It does not manufacture or execute a
final ADQL statement and does not prove VizieR availability. Allowlisted Connector policy,
credential references, timeout/retry/rate-limit/cache behavior, concrete ADQL translation, network
execution, and returned-source assessment belong to M05.

## Verification

- `uv run scidatafusion phase2-plan-demo --goal "Study Type Ia supernova light curves using
  multi-source data integration into CSV." --confirmed-by "demo-reviewer"` runs the complete
  offline M00-M04 demonstration and emits a privacy-reduced plan summary.
- `uv run pytest tests/test_search_planning.py -q --no-cov` covers the confirmed Ia plan, all four
  source categories, VizieR adapter semantics, field/source coverage, explicit budget deferral,
  zero-capability fail-closed behavior, partial capability gaps, query normalization/deduplication,
  stopping-policy boundaries, replay/concurrency, integrity tampering, and strict registry loading.
- The repository phase gate runs Ruff, format checking, strict mypy, branch-aware pytest, Bandit,
  the secret scan, and dependency audit before this checkpoint is pushed.

## Metric interpretation

M04 reports artifact-derived family, active-family, query, source-category, coverage-cell, gap,
deferred-query, and deduplicated-query counts. Fixture tests prove contract invariants and the Ia
golden path; they do not establish the V4 target `SearchPlan` executability rate of at least 98%,
query-intent coverage, invalid-query rate, or planning P95. Those claims require a versioned
benchmark corpus, sample counts, baselines, and confidence intervals after M05 provides executable
Connectors.

## Known boundary

The M04 registry is configuration, not a live service-health registry, and the injected availability
snapshot is caller-supplied. Planning replay is process-local until durable artifact storage and
cross-worker idempotency are implemented. Real source discovery begins in M05. The competition's
credentialed Qwen invocation proof also remains pending; Mock-backed provider tests and deterministic
fallbacks are not a substitute for that deployment acceptance item.
