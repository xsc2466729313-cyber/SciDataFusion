# ADR-0011: Evidence-first explicit table extraction

## Status

Accepted and implemented for the first M13 offline slice.

## Context

M10 preserves native CSV values and exact cell byte spans but deliberately does not decide which
cells satisfy the scientific data contract. M13 must produce usable field candidates without
inventing values, losing source context, or allowing a candidate to exist before its evidence.
The broader specification accepts DocumentIR, FigureIR, and DatasetIR and permits carefully
validated semantic extraction. Those inputs are not yet available in the accepted vertical slice.

## Decision

1. The first M13 runtime accepts only quality-passed, one-header-row M10 TableIR and runs offline
   with a versioned deterministic exact-header rule. It performs no alias mapping, inference,
   derivation, model execution, external request, normalization, or Gold write.
2. A non-empty data cell becomes an `EvidenceAtom` before it can become a field candidate. The atom
   references the exact Bronze object, TableIR, cell, byte span, lexical text, decoded text, and
   hashes needed to replay the observation.
3. A candidate is created only when its header exactly equals a non-derived contract field. It
   retains the unmodified decoded text and binds both its primary value evidence and all entity-key
   evidence from the same table row.
4. Missing entity-key values block extraction for that row. Empty required values, missing exact
   required headers, unsupported header structures, failed table quality, and unmapped headers
   remain explicit gaps. Unknown headers are tied to their exact header cell so separate gaps have
   separate content-derived identities.
5. Request, rule, runtime, atoms, candidates, sets, gaps, aggregate result, and completion event are
   strictly validated and content addressed. Complete results use immutable canonical checkpoints;
   concurrent callers share one cancellation-isolated execution.
6. The `field.extracted` event and CLI summary contain hashes and aggregate counts, not raw values,
   lexemes, source locations, URLs, research text, or reviewer identity.

## Consequences

- Every emitted candidate has replayable table-cell evidence and same-row entity evidence.
- Exact-header matching is deliberately conservative: `mjd` is not silently treated as
  `observation_time`; M14 owns registered aliases and semantic alignment.
- Required-field coverage describes fields observed across the accepted result, not Gold
  completeness or row-level fitness for analysis.
- This checkpoint proves a native-table vertical slice only. DocumentIR, complex tables, FigureIR,
  DatasetIR, semantic relations, units, times, and judged-corpus precision/recall remain unproven.
- Process-local checkpoints demonstrate canonical replay and conflict rejection, not durable
  production storage.
