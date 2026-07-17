# ADR 0034: Current-topic structured previews

Status: Accepted for v1.5.0.

## Context

Online research could discover and content-address real current-topic files, but CSV, TSV, and JSON artifacts were available only as raw downloads. The workbench therefore could not show whether a downloaded object contained useful rows and columns without crossing the unsupported boundary into semantic field mapping.

## Decision

1. Verify every artifact against its acquired SHA256 before parsing.
2. Accept only UTF-8 CSV, TSV, and scalar-record JSON. Reject duplicate or whitespace-mutated headers, ragged rows, nested JSON cells, non-standard numbers, and structures beyond fixed row, column, cell, and preview limits.
3. Use structured CSV/JSON parsers and a Polars string frame. Preserve CSV values as JSON strings and JSON numbers as their original lexical tokens, including values such as `1.20`.
4. Bind every preview cell to artifact hash, one-based row/column location, original column name, and deterministic evidence identity.
5. Project datasets, columns, and evidence into the Chinese workbench and evidence graph. Keep the immutable raw artifact directly downloadable.
6. Treat the output as a preview, not a semantic mapping or Gold dataset. No unit conversion, alias resolution, conflict selection, missing-value repair, or scientific-value generation occurs in M27.

## Consequences

- Users can immediately inspect useful current-topic records instead of seeing only search-result metadata.
- Unsupported documents remain valuable raw evidence and receive a visible parser-status record rather than being silently discarded.
- Wide or long datasets expose exact total dimensions while returning only a bounded preview.
- Formal CSV/Parquet publication remains blocked until later mapping, normalization, fusion, and quality gates bind required fields to evidence.
