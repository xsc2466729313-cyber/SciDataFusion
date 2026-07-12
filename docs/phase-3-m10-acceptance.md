# M10 acceptance: native table IR and cell evidence

## Status

Accepted on 2026-07-12 for the first deterministic native-CSV slice. This is not acceptance of
PDF/image/XLSX/HTML table recovery or the representative table benchmark.

## Definition of Done

- [x] Strict frozen Pydantic v2 contracts with `extra="forbid"` cover request, policy, runtime,
  parser identity, `TableIR`, `CellIR`, header hierarchy, quality report, attempt, gap, route result,
  aggregate metrics, result, and `table.parsed` event.
- [x] M10 verifies the exact M08 request/result/plan/registry/runtime/event and the complete M07
  Bronze lineage and bytes before parsing.
- [x] Exactly the M08 M10 routes are considered. The Ia M09 document routes, archive metadata-only
  entry, M11 chart work, and M13 field extraction receive zero M10 calls.
- [x] CSV parsing is deterministic, bounded, UTF-8 strict, network-free, model-free, and supports
  comma/TSV delimiters, BOM, quoting, escaped quotes, quoted newlines, and empty cells.
- [x] Every cell retains its exact source object, byte hash and byte span, exact lexeme, decoded
  text, representation hashes, grid location, role, spans, inferred type label, and confidence.
- [x] Normalization independently rechecks every source span and decoded lexeme. A parser cannot
  invent or mutate a scientific value through its output contract.
- [x] Rectangular structure, strict output schema, unique header, and cell-evidence quality gates
  are deterministic. A failed header gate retains the table and returns an explicit review gap.
- [x] TableIR has stable immutable text-row and all-String Polars projections. No projection coerces
  a scientific-looking string into a numeric value.
- [x] TableIR storage is canonical and content addressed. Complete results replay from an immutable
  canonical checkpoint, identical calls share a result, and conflicts fail closed.
- [x] Nested identities, aggregate sets, metrics, event causality, and output hashes are
  independently recomputed during replay.
- [x] The privacy-reduced CLI reports hashes, statuses, parser IDs, quality counts, and aggregate
  dimensions without cell values, URLs, source names, reviewer identity, or credentials.
- [x] Architecture and acceptance documentation state parser, evidence, projection, event,
  idempotency, security, and downstream boundaries.
- [x] The final repository gate passes Ruff, format checking, strict mypy, pytest coverage, Bandit,
  secret scanning, and dependency auditing after all M10 files and documentation are complete.

## Offline Ia evidence

`phase3-table-demo` executes the packaged M00-M08 chain and the one planned native CSV route. The
measured result is:

- aggregate status `succeeded` in offline mode;
- one eligible route, one successful attempt, one TableIR, and one `table.parsed` event;
- two rows, four columns, eight cells, and eight exact cell evidence anchors;
- all output-schema, table-structure, and cell-evidence gates pass;
- zero gaps, model attempts, network attempts, cost, Bronze writes, and M13 extractions;
- numeric-looking source values remain strings in TableIR and the Polars projection.

Malformed adapter output produces a structured review result without a table. Duplicate headers
retain the complete content-addressed table, fail the structure gate, and produce a review gap.
Unavailable runtime capability produces a blocked attempt with no engine identity or claimed work.

## Verification

```powershell
uv run pytest tests/test_table_csv.py tests/test_table_service.py tests/test_cli.py -q --no-cov
uv run scidatafusion phase3-table-demo `
  --goal "Study Type Ia supernova light curves" --confirmed-by "m10-acceptance"
powershell -ExecutionPolicy Bypass -File scripts/check.ps1
```

The complete worktree gate passed on 2026-07-12 with 548 tests and 90.03% total coverage. Ruff,
format checking, strict mypy over 148 source files, Bandit, the repository secret scanner over 550
files, and dependency auditing all passed; the audit reported no known vulnerabilities. The M10
CLI also exited successfully with the measured offline result above.

## Known boundaries

- Native CSV has no page image or bbox. The eight cells are located by exact Bronze byte spans, not
  fabricated page geometry. The broader requirement that image-derived cells locate their source
  region remains deferred.
- XLSX, HTML tables, PDF table regions, image/vision tables, merged cells, footnotes, multi-level
  headers, borderless tables, rotation, and cross-page merging are not implemented in this slice.
- The first-row header rule is deterministic but intentionally conservative. Duplicate or empty
  headers require review and are never silently repaired.
- Polars projection supports zero or one header row. Multi-level header flattening needs an explicit
  versioned policy and must preserve the hierarchy.
- No LLM is used. Future semantic header or footnote candidates must be strictly validated and may
  not alter cells or bypass deterministic quality gates.
- The Table Structure F1, cell accuracy, header F1, and cross-page merge targets require the stated
  100-table representative benchmark. One eight-cell fixture cannot establish those values.
- M10 produces Silver TableIR only. It does not create `EvidenceAtom`, map contract fields, normalize
  units/times, resolve entities, fuse conflicts, or claim Gold correctness.
