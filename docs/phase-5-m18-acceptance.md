# M18 acceptance: contract quality gates and review planning

## Status

Accepted on 2026-07-13 for the first deterministic offline audit slice. This is not acceptance of
domain validators, statistical anomaly detection, automatic retry execution, conversion repair,
reviewer resolution, rollback, final export, or benchmark-backed quality recall and repair accuracy.

## Definition of Done

- [x] Strict frozen Pydantic v2 contracts with `extra="forbid"` cover request, policy, runtime,
  gate evaluations, issues, repair plan, review queue, report, formal Gold, metrics, result, and
  completion event.
- [x] M18 re-verifies the exact M13-M17 request/result/event chain, contract, nested hashes,
  evidence references, and Bronze bytes before execution or replay.
- [x] Every confirmed-contract quality gate is evaluated exactly once against every Gold candidate
  record with deterministic counts, score, threshold, pass state, and evidence references.
- [x] Every failed record-level gate produces one Issue with severity, evidence, suggested action,
  affected fields, structured code, and open status.
- [x] Every Issue has exactly one whitelist repair-plan step and one pending review item with
  bounded impact and retry count.
- [x] Repair comparison records unchanged before/after quality because no action executes; there is
  no fabricated quality improvement.
- [x] Formal Gold can exist only after at least one record and all blocking gates pass.
- [x] Complete results are immutable, checkpointable, single-flight, cancellation isolated,
  tamper evident, and emit one causally linked `quality.gated` event.
- [x] No automatic repair, LLM repair decision, network access, final export, or scientific-value
  mutation occurs.
- [x] Architecture, limitations, metrics, verification commands, and offline evidence are recorded.
- [x] Final repository gates and measured totals are recorded after the complete M18 run.

## Offline Ia evidence

`phase5-audit-demo` executes the packaged M00-M18 chain without external services. The one M17
Gold candidate record fails all three confirmed-contract gates: required-field completeness,
required-field provenance, and the photometric any-of requirement. M18 emits three Critical
issues, three `request_human` repair steps, and three pending review items. No repair executes, the
quality score remains 0.0 before and after, and no formal Gold dataset is produced.

This is an honest functional proof of deterministic gating and review routing. It cannot establish
quality-issue detection recall, false-positive rate, automatic-repair accuracy, average quality
improvement, or reviewer turnaround. Those metrics need labeled error-injection corpora and
representative multi-domain data.

The CLI contains only aggregate counts, gate/issue/action classes, opaque identifiers, and hashes.
It omits field names, scientific values, evidence content, goal text, and reviewer identity.

## Verification

```powershell
uv run pytest tests/test_quality_service.py tests/test_cli.py -q --no-cov
uv run scidatafusion phase5-audit-demo `
  --goal "Study Type Ia supernova light curves using multi-source data integration into CSV." `
  --confirmed-by "m18-acceptance"
powershell -ExecutionPolicy Bypass -File scripts/check.ps1
```

The demo command intentionally returns the review-required process code because the scientific
quality gate fails; its JSON result remains a successful, validated M18 audit artifact.

The final repository gate completed with 610 passing tests and 90.05% total coverage. Ruff and
format checks passed across 196 files, mypy passed across 195 source files, Bandit reported no
issues, the secret scan passed across 656 files, and the dependency audit reported no known
vulnerabilities.

## Known boundaries

- The packaged Ia route has one Gold candidate record and three deterministic gate failures. It is
  not a representative quality benchmark.
- Range, unit, uniqueness, duplicate, chart-axis/legend, uncertainty, source-trust, and statistical
  anomaly validators remain deferred until their evidence and Domain Pack semantics are available.
- Retry actions are represented in the whitelist contract but are not executed. The first slice
  plans only `request_human` because no safe local action can reconstruct the missing scientific
  context.
- Human review authentication, pause/resume persistence, decisions, audit signatures, and timeout
  escalation require an API/workbench slice.
- Formal Gold is a gated artifact contract, not an exported CSV/Parquet delivery. M20 owns delivery.
