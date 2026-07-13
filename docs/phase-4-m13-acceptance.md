# M13 acceptance: evidence-first explicit field extraction

## Status

Accepted on 2026-07-13 for the first deterministic native-table slice. This is not acceptance of
document, figure, scientific-format, semantic, normalization, or Gold extraction.

## Definition of Done

- [x] Strict frozen Pydantic v2 contracts with `extra="forbid"` cover request, policy, runtime,
  evidence atoms and sets, field candidates and sets, gaps, metrics, result, and event payload.
- [x] M13 re-verifies the exact confirmed scientific contract, M10 request/result/event, M08 plan,
  M07 Bronze lineage, nested identities, and raw source bytes before extraction or replay.
- [x] Every candidate is created after and references at least one exact cell `EvidenceAtom`; its
  value, cell, table, contract field, and hashes must all match independently verified inputs.
- [x] Every candidate is bound to explicit entity-key evidence from the same row. A missing entity
  value blocks the row rather than permitting cross-row or inferred identity.
- [x] The accepted rule uses exact contract headers only, retains raw decoded values unchanged,
  and emits only `explicit` candidates. Alias, inferred, derived, unit, and time processing are off.
- [x] Failed quality gates, unsupported headers, missing required fields, empty required cells,
  entity failures, and unknown headers are explicit, content-addressed gaps.
- [x] Policy bounds tables, rows, evidence atoms, candidates, and checkpoint bytes. No network,
  model, cost, Bronze mutation, M14 mapping, or Gold write occurs.
- [x] Complete results are canonical, immutable, checkpointable, single-flight, cancellation
  isolated, tamper evident, and emit one causally linked `field.extracted` event.
- [x] The CLI exposes only aggregate counts, safe identifiers, hashes, statuses, and gap codes.
- [x] Architecture, known boundaries, verification commands, and measured acceptance evidence are
  documented.
- [x] The repository gate passes Ruff, formatting, strict mypy, pytest coverage, Bandit, secret
  scanning, and dependency auditing after all M13 work is complete.

## Offline Ia evidence

`phase4-extract-demo` runs the packaged M00-M10 chain and M13 without external services. It
produces one accepted table row, four exact table-cell evidence atoms, and four explicit candidates
for `object_id`, `observation_time`, `band`, and `magnitude`. Evidence and entity-binding coverage
are 100%. Required-field coverage is 75% because the source table has no exact
`source_record_id` header; the aggregate correctly returns `partial` with one explicit gap.

The run performs zero network requests, model calls, inferred or derived candidates, cost, Gold
writes, M14 mappings, and Bronze writes. Empty required cells retain other supported candidates and
record a gap. Alias headers remain unmapped and each unknown header gap references its exact source
cell. Missing entity values block the whole row.

## Verification

```powershell
uv run pytest tests/test_extraction_service.py tests/test_cli.py -q --no-cov
uv run scidatafusion phase4-extract-demo `
  --goal "Study Type Ia supernova light curves" --confirmed-by "m13-acceptance"
powershell -ExecutionPolicy Bypass -File scripts/check.ps1
```

The complete worktree gate passed on 2026-07-13 with 564 tests and 90.00% total coverage. Ruff,
format checking over 156 files, strict mypy over 155 source files, Bandit, the repository secret
scanner over 566 files, and dependency auditing all passed; the audit reported no known
vulnerabilities. The M13 CLI also exited successfully with the measured offline result above.

## Known boundaries

- This slice consumes only M10 native TableIR with one exact header row. M09 DocumentIR, M11
  FigureIR, M12 scientific formats, merged or hierarchical tables, and cross-page evidence are not
  extracted.
- Exact header equality is not semantic mapping. Aliases, ontology alignment, ambiguity scoring,
  and user-approved mapping belong to M14.
- Values remain strings exactly as decoded by M10. Numeric parsing, units, time scales, coordinate
  systems, uncertainty, and transformation provenance belong to M15 and later phases.
- The fixture demonstrates evidence completeness, not extraction precision/recall on a labeled
  representative corpus. Competition accuracy claims require a versioned benchmark.
- `partial` is intentional: M13 neither invents the absent `source_record_id` nor promotes the
  result to Gold. Conflict-preserving fusion and row-level Gold gates remain downstream.
