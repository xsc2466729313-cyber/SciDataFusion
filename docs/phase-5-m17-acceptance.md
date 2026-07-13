# M17 acceptance: exact-consensus conflict-preserving fusion

## Status

Accepted on 2026-07-13 for the first deterministic offline slice. This is not acceptance of
tolerance aggregation, uncertainty weighting, source priority, version precedence, experiment-
condition separation, LLM conflict explanation, final Gold publication, or benchmark-backed
conflict accuracy.

## Definition of Done

- [x] Strict frozen Pydantic v2 contracts with `extra="forbid"` cover request, policy, runtime,
  retained candidates, fused records, conflicts, decisions, Gold candidates, metrics, result, and
  completion event.
- [x] M17 re-verifies the exact M13-M16 request/result/event chain, contract, nested hashes,
  evidence references, and Bronze bytes before execution or replay.
- [x] Every normalized field in every resolved record becomes one immutable fusion candidate; raw
  and normalized values and upstream issue context are retained without mutation.
- [x] A single value is selectable only when M16-eligible; multi-candidate selection requires exact
  eligible consensus across normalized value hashes.
- [x] Distinct values create an unresolved conflict with no selected fused value, so conflicts
  cannot be silently overwritten.
- [x] Every Gold candidate field references its decision, selected candidate, all retained
  candidates, and at least one upstream EvidenceAtom.
- [x] Fusion policy and rule identities are configurable, versioned, content-addressed, and
  replayable.
- [x] Complete results are immutable, checkpointable, single-flight, cancellation isolated,
  tamper evident, and emit one causally linked `fusion.completed` event.
- [x] No tolerance aggregation, source-priority selection, uncertainty weighting, LLM value
  decision, network access, final Gold publication, or scientific-value mutation occurs.
- [x] Architecture, limitations, metrics, verification commands, and offline evidence are recorded.
- [x] Final repository gates and measured totals are recorded after the complete M17 run.

## Offline Ia evidence

`phase5-fuse-demo` executes the packaged M00-M17 chain without external services. The one M16
singleton contributes four field candidates. `object_id` and `band` are evidence-complete and
eligible, so each receives a deterministic single-candidate decision. `observation_time` and
`magnitude` retain M15 context issues and are explicitly withheld. The result contains one fused
record, two Gold candidate fields, two withheld fields, zero conflicts, and zero silent overwrites.

This is an honest functional proof of traceability and no-overwrite behavior. It cannot establish
conflict-detection recall, conflict-classification accuracy, aggregation error, or review rate.
Those metrics need a labeled multi-source corpus with compatible uncertainty ranges, genuine
measurement disagreement, source/version conflicts, and non-comparable experimental conditions.

The CLI contains only aggregate decisions, metrics, opaque identifiers, and hashes. It omits field
names, raw and normalized scientific values, evidence content, goal text, and reviewer identity.

## Verification

```powershell
uv run pytest tests/test_fusion_service.py tests/test_cli.py -q --no-cov
uv run scidatafusion phase5-fuse-demo `
  --goal "Study Type Ia supernova light curves using multi-source data integration into CSV." `
  --confirmed-by "m17-acceptance"
powershell -ExecutionPolicy Bypass -File scripts/check.ps1
```

The final repository gate completed with 603 passing tests and 90.05% total coverage. Ruff and
format checks passed across 188 files, mypy passed across 187 source files, Bandit reported no
issues, the secret scan passed across 638 files, and the dependency audit reported no known
vulnerabilities.

## Known boundaries

- The packaged Ia route has one record, so it does not produce a real multi-source conflict. Pure
  rule and contract tests verify exact consensus and distinct-value preservation, but they are not
  a substitute for a labeled integration corpus.
- Numeric tolerance, uncertainty intervals, significant-figure reconciliation, temporal/version
  precedence, and source trust are disabled until registered domain rules provide their semantics.
- M17 cannot infer that measurements were collected under different experimental conditions. Such
  separation requires evidence-backed condition fields or a validated Domain Pack rule.
- LLM conflict explanations are disabled. A future adapter may propose explanations only under
  strict schema validation and cannot select or mutate values.
- `GoldCandidateDataset` is an auditable candidate view. M18 quality audit and review must run
  before any final Gold export claim.
