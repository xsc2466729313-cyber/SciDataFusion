# ADR 0035: Reviewable field mapping and evidence-table export

Status: Accepted for v1.6.0.

## Context

M27 exposed verified current-topic rows and columns, but users still had to interpret every source header manually and could only download individual raw artifacts. A useful cross-source result needs a common target vocabulary and cell-level provenance without allowing a model to rewrite scientific values.

## Decision

1. Build the allowed target vocabulary only from the current task's validated exploration profile.
2. Resolve normalized exact-name matches deterministically. Send only the research goal, allowed target names, artifact hash, column index, and source column name to Qwen for unresolved columns. Never send cell values.
3. Validate the complete model response with strict Pydantic contracts. Reject missing columns, duplicate decisions, unknown artifacts, unknown targets, altered source names, duplicate targets within one dataset, and mappings below the confidence threshold.
4. Preserve every rejected or uncertain column as `unmapped`; retain its source name and raw values without repair or substitution.
5. Re-read immutable Bronze bytes, verify SHA256 again, and export a UTF-8 evidence long table. Each cell row contains source URL, artifact hash, source row and column, JSON-encoded raw value, deterministic evidence ID, and the reviewed mapping decision.
6. Bound parsing to 250,000 cells per artifact and the generated CSV to 64 MiB. Keep this output explicitly non-Gold until downstream normalization, conflict, unit, and quality gates pass.

## Consequences

- Arbitrary research topics can produce an immediately usable, machine-readable cross-source table even when some semantics remain unresolved.
- Model failures reduce mapping coverage but cannot corrupt, invent, or hide source data.
- The evidence graph can connect source columns to planned target fields while preserving the exact evidence basis for every edge.
- Consumers can filter or pivot the long table safely and decide whether to accept, revise, or reject each semantic mapping.
