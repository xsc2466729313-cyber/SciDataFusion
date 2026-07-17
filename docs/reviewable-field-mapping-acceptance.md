# M28 acceptance: reviewable field mapping and evidence export

## Definition of Done

- [x] Every parsed source column receives exactly one immutable mapped or unmapped decision.
- [x] Exact matches are deterministic; Qwen receives only the research goal and field names, never scientific cell values.
- [x] Model output is strict, complete, allowlisted, confidence-gated, and fails closed to an unmapped source field.
- [x] Dataset-local target collisions cannot silently overwrite or merge source columns.
- [x] Every exported cell retains source URL, artifact SHA256, source row and column, JSON raw value, evidence ID, and mapping status.
- [x] Bronze bytes are hash-verified again at export; the API returns a content digest and UTF-8 BOM CSV.
- [x] Per-artifact cell count and generated-byte limits prevent unbounded memory use.
- [x] The Chinese React workbench displays mapping coverage, rationale tooltips, confidence, unmapped fields, graph edges, and evidence-table download.
- [x] Ruff, mypy, pytest with coverage, Bandit, secret scan, dependency audit, frontend build, Compose smoke, browser checks, and Windows clean-directory smoke pass for the final commit.
