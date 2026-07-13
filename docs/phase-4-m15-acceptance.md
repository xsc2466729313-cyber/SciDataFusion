# M15 acceptance: evidence-preserving scientific normalization

## Status

Accepted on 2026-07-13 for the first exact-decimal, no-guess offline slice. This is not acceptance
of unit, time-scale, coordinate, identifier, category, missing-value, or uncertainty conversion
without evidenced source context.

## Definition of Done

- [x] Strict frozen Pydantic v2 contracts with `extra="forbid"` cover request, policy, runtime,
  normalized fields/records/sets, transformations, issues, metrics, result, and event payload.
- [x] M15 re-verifies the exact M13-M14 request/result/event chain, contract, evidence references,
  nested hashes, and Bronze bytes before execution or replay.
- [x] Every M14 mapping is retained. Raw values and hashes remain unchanged; no conflict or
  scientifically ambiguous value is silently overwritten or deleted.
- [x] Every non-identity decimal parse has a content-addressed `TransformationRecord` with formula,
  runtime library/version, exact input/output, precision metadata, reversibility, and evidence.
- [x] Missing source units and time scales create explicit blocking issues. Target units are never
  treated as source evidence and no false scientific conversion is reported.
- [x] Complete results are immutable, checkpointable, single-flight, cancellation isolated,
  tamper evident, and emit one causally linked `record.normalized` event.
- [x] The privacy-reduced CLI exposes only aggregate state, counts, identifiers, and hashes.
- [x] No LLM value mutation, network access, unit/time guessing, cost, or Gold write occurs.
- [x] Architecture, known boundaries, verification commands, and measured evidence are documented.
- [x] Final repository gates and measured totals are recorded below after the complete M15 run.

## Offline Ia evidence

`phase4-normalize-demo` runs the packaged M00-M15 chain without external services. Four accepted
M14 mappings become one normalized record with four retained fields. `observation_time=59000.1`
and `magnitude=12.3` are parsed as exact decimals and each receives one reversible transformation
record. Their lexical precision remains one decimal place; no binary float is introduced.

The source table does not evidence that the time is MJD, does not state a time scale, and does not
state that magnitude uses `mag`. M15 therefore emits two `source_unit_missing` issues and one
`time_scale_missing` issue. Identifier and band are eligible for M16; time and magnitude remain
reviewable. The aggregate is correctly `partial`.

The CLI summary contains none of the raw/normalized values, units, research goal, source content,
or reviewer identity. The run performs zero model, network, LLM-mutation, Gold, and cost operations.

## Verification

```powershell
uv run pytest tests/test_normalization_service.py tests/test_cli.py -q --no-cov
uv run scidatafusion phase4-normalize-demo `
  --goal "Study Type Ia supernova light curves" --confirmed-by "m15-acceptance"
powershell -ExecutionPolicy Bypass -File scripts/check.ps1
```

The complete worktree gate passed on 2026-07-13 with 587 tests and 90.06% total coverage. Ruff and
format checking covered 172 files, strict mypy checked 171 source files, Bandit reported no issues,
the repository secret scanner checked 602 files, and dependency auditing reported no known
vulnerabilities. The M15 CLI also exited successfully with the measured offline result above.

## Known boundaries

- Source-unit and time-scale annotations are not yet emitted by M10/M13. Deterministic Astropy,
  Pint, or pyproj conversions must wait for evidence-backed annotations instead of guessing.
- The current slice handles exact numeric parsing and string identity only. Calendar/timezone,
  coordinate reference systems, uncertainty propagation, category registries, identifier
  canonicalization, and missing-value policies remain explicit future Domain Pack capabilities.
- Exact decimal strings are typed by `NormalizedValueKind.DECIMAL`; downstream storage must retain
  decimal semantics and must not silently cast them to binary float.
- This fixture does not establish representative-corpus correctness, unit coverage, round-trip
  error, or latency targets. Those require labeled multi-domain benchmark data.
- M15 does not resolve entities, select among conflicting observations, write Gold, or repair
  source data. Those responsibilities begin in M16 and later checkpoints.
