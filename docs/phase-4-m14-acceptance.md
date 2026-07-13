# M14 acceptance: evidence-backed canonical field mapping

## Status

Accepted on 2026-07-13 for the first deterministic exact-field slice. This is not acceptance of
automatic alias, ontology, embedding, LLM, complex-header, normalization, or Gold mapping.

## Definition of Done

- [x] Strict frozen Pydantic v2 contracts with `extra="forbid"` cover request, policy, runtime,
  rules, mapping evidence, mappings, unmapped fields, sets, metrics, result, and event payload.
- [x] M14 re-verifies the exact M13 request/result/event, confirmed contract, M10 lineage, nested
  identities, evidence atoms, entity bindings, and Bronze bytes before mapping or replay.
- [x] Every mapping records method, rule version/hash, score, threshold, type compatibility,
  contract field hash, source candidate, value/entity evidence, decision, and M15 eligibility.
- [x] Automatic acceptance is derived from type compatibility and threshold; it cannot be toggled
  independently or used to mutate a scientific value.
- [x] Unknown headers are retained in a content-addressed `UnmappedFieldSet` with exact source-cell
  lineage. Registered aliases are suggestions only when upstream value evidence is absent.
- [x] Policy bounds mappings, unmapped fields, and checkpoint bytes. No model, embedding, network,
  normalization, Gold write, scientific-value change, or cost occurs.
- [x] Complete results are canonical, immutable, checkpointable, single-flight, cancellation
  isolated, tamper evident, and emit one causally linked `field.mapped` event.
- [x] The privacy-reduced CLI reports safe aggregate metrics, methods, decisions, field names,
  identifiers, and hashes without values, unknown header text, URLs, or reviewer identity.
- [x] Architecture, known boundaries, verification commands, and measured acceptance evidence are
  documented.
- [x] The repository gate passes Ruff, formatting, strict mypy, pytest coverage, Bandit, secret
  scanning, and dependency auditing after all M14 work is complete.

## Offline Ia evidence

`phase4-map-demo` runs the packaged M00-M14 chain without external services. M14 receives four
M13 candidates and produces four exact canonical mappings plus four independent mapping-evidence
records. All four mappings pass type compatibility and the 1.0 automatic threshold and become
eligible for M15. Mapping evidence coverage and automatic acceptance are both 100%.

The missing `source_record_id` remains an upstream gap, so the aggregate correctly stays
`partial`. A separate integration fixture replaces source headers with registered aliases `mjd`
and `filter`: M14 retains each unknown header with distinct source-cell lineage and suggests
`observation_time` and `band`, but does not auto-map either because M13 has no value evidence for
those source fields.

The run performs zero model, embedding, network, normalization, Gold, Bronze-write, and cost
operations. Raw values, source header text, locators, research goals, and reviewer identities are
absent from the CLI summary and completion event.

## Verification

```powershell
uv run pytest tests/test_mapping_service.py tests/test_extraction_service.py tests/test_cli.py -q --no-cov
uv run scidatafusion phase4-map-demo `
  --goal "Study Type Ia supernova light curves" --confirmed-by "m14-acceptance"
powershell -ExecutionPolicy Bypass -File scripts/check.ps1
```

The complete worktree gate passed on 2026-07-13 with 579 tests and 90.04% total coverage. Ruff,
format checking over 164 files, strict mypy over 163 source files, Bandit, the repository secret
scanner over 584 files, and dependency auditing all passed; the audit reported no known
vulnerabilities. The M14 CLI also exited successfully with the measured offline result above.

## Known boundaries

- M13 currently emits value candidates only for exact canonical headers. M14 therefore cannot
  safely auto-map alias-only headers until an upstream source-field observation contract provides
  value evidence and same-row entity binding.
- Alias suggestions use aliases already compiled into the confirmed contract and exact
  case-insensitive equality. They do not claim ontology or multilingual semantic equivalence.
- Embedding retrieval, reranking, LLM judgment, complex/multi-level headers, footnotes, ambiguity
  fusion, and human-confirmed rule publication are not implemented in this slice.
- M10 type labels are conservative checks, not numeric conversion. Units, time scales, coordinate
  systems, uncertainty, and transformation records belong to M15.
- The specified accuracy, Macro-F1, erroneous-auto-map, unmapped-recall, and explanation targets
  require a labeled representative benchmark. Four exact mappings cannot establish them.
- M14 produces mapping artifacts only. It does not mutate M13 candidates, write Gold, normalize
  values, resolve entities, or fuse conflicts.
