# ADR-0010: Native TableIR and exact cell evidence

## Status

Accepted and implemented for the first M10 offline slice.

## Context

M08 plans one native `m10.csv` route in the Ia fixture, while M09 intentionally ignores it. M10
must recover a table without changing Bronze bytes, inventing values, treating inferred types as
scientific transformations, or losing the location of an individual cell. The broader M10
specification also covers XLSX, HTML/PDF/image tables, merged cells, multi-level headers, footnotes,
and cross-page merging. Those formats need separate representative fixtures and parser decisions;
claiming them from one small CSV would be misleading.

CSV has logical values and lexical source representations. For example, `"a,b"` logically decodes
to `a,b`, but both forms are needed: downstream consumers need the decoded text, while audit and
repair need the exact bytes including quotes and escapes. Empty fields have a valid zero-length
source span at their delimiter boundary.

## Decision

1. M10 consumes and re-verifies the exact M08 request, result, plan, registry, runtime, event, M07
   metadata, and Bronze bytes. It executes only routes whose target module is M10. M09 output is not
   required for a native CSV route.
2. The first runtime exposes only deterministic `m10.csv`. CSV and TSV media types select a fixed
   delimiter; UTF-8 and UTF-8 BOM are supported. No encoding guess, network call, model call, or
   external document instruction is allowed.
3. Each `CellIR` retains row and column, spans, role, exact source byte range, exact CSV lexeme,
   decoded text, hashes of both representations, and a parser confidence. Type detection produces
   only an `inferred_kind` label; the authoritative value remains text.
4. The lexical scanner handles quoted delimiters, escaped quotes, and quoted line endings. The
   normalizer independently decodes every bounded lexeme and rejects adapter drift before creating
   a cell identity.
5. The first row is a deterministic header candidate. Empty or duplicate headers fail the table
   structure gate. The complete table and all cell text remain retained and the route becomes
   `needs_review`; the system does not silently rename columns.
6. Table, cell, header, quality, attempt, route, aggregate, and event identities are content
   derived. Canonical JSON TableIR storage and complete-result checkpoints reject overwrite,
   noncanonical replay, and nested hash drift.
7. `table_to_rows` projects any valid grid to immutable text rows. `table_to_polars` creates an
   all-String Polars DataFrame only for no-header or one-level unique-header tables. It never
   performs numeric coercion. Multi-level flattening requires an explicit future policy.
8. M10 emits exactly one privacy-reduced `table.parsed` event containing hashes, status, and counts,
   not cell content, URLs, filenames, credentials, or scientific values.

## Consequences

- The Ia CSV has eight cells and eight exact byte anchors, so fixture evidence coverage is 100%.
- Quoting is decoded reversibly while the original lexeme remains independently reconstructable.
- Polars consumers receive stable text columns and cannot mistake type probing for normalization.
- The first slice cannot locate cells in a page image because native CSV has no page canvas. PDF,
  image, XLSX, HTML tables, merged cells, footnotes, multi-level headers, and cross-page merging are
  deferred and must not reuse this acceptance result as proof.
- Process-local TableIR storage and checkpoints prove content addressing and replay semantics, not
  production object-store durability.
- The required structure, content, header, and cross-page benchmark targets remain unproven until a
  labeled representative corpus is executed with sample size and confidence intervals.
