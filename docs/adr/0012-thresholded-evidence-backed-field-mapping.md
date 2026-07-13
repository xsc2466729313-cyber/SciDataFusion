# ADR-0012: Thresholded evidence-backed field mapping

## Status

Accepted and implemented for the first M14 offline slice.

## Context

M13 produces candidates only when a source header exactly equals a contract field and every value
has replayable cell evidence. It records unknown or alias headers as source-cell-bound gaps but does
not create value candidates for them. M14 must not turn a header-only alias suggestion into a
scientific field mapping because that would bypass the evidence-first boundary.

The broader M14 specification includes ontology matching, embeddings, reranking, LLM judgment,
complex headers, units, and benchmark targets. The accepted vertical slice has no evaluated
embedding/model capability or value-evidenced alias candidate, so those paths cannot be claimed.

## Decision

1. M14 consumes and re-verifies the exact M13 request, result, event, contract, M10 lineage, and
   Bronze bytes before mapping or checkpoint replay.
2. The first rule maps only a candidate whose existing source field exactly equals its canonical
   contract field. It independently verifies the field-contract hash, non-mutating M10 value-kind
   compatibility, primary evidence, entity evidence, score, and configured threshold.
3. `MappingEvidence` and `FieldMapping` are separate content-addressed records. Every mapping names
   its method, rule version/hash, score, threshold, type decision, evidence references, and M15
   eligibility. Eligibility is derived and cannot be set independently.
4. Each M13 unknown-header gap becomes an `UnmappedField` tied to the exact table and header cell.
   Exact case-insensitive matches against aliases already compiled into the confirmed contract are
   retained as suggestions only. No source header text or source value is copied into M14 output.
5. Alias auto-mapping, embeddings, models, external network, and value transformation are disabled
   by literal policy/runtime contracts. Enabling them requires new evidence-bearing input contracts,
   versioned capabilities, representative benchmarks, and a separate acceptance decision.
6. Complete results and the `field.mapped` event are content derived, causally linked, canonical,
   immutable, checkpointable, single-flight, and cancellation isolated.

## Consequences

- Every accepted mapping is explainable and has a one-to-one `MappingEvidence` record.
- Unknown fields are visible and auditable rather than deleted, even when only a header is known.
- Registered aliases such as `mjd` and `filter` can be suggested, but they cannot enter M15 until
  M13 supplies value-level evidence for those source fields.
- The Ia exact-field fixture has 100% mapping evidence and automatic acceptance, but the result is
  still partial because `source_record_id` is absent upstream.
- This slice does not establish the specified mapping accuracy, Macro-F1, error rate, or unmapped
  recall targets; those require a versioned labeled corpus with sample sizes and confidence bounds.
