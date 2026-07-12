# ADR-0006: Evidence-backed, candidate-only source selection

## Status

Accepted for the M06 coverage and source-selection checkpoint.

## Context

M05 emits normalized discovery candidates and evidence-backed metadata claims. Those claims can
identify promising sources, but they do not prove that a downloaded file contains valid rows or
that a scientific value satisfies a contract constraint. M06 must maximize useful coverage and
source diversity without promoting discovery metadata to parsed scientific truth.

The decision must also be replayable. A coverage report assembled from unrelated contract, plan,
or candidate snapshots could appear internally consistent while being false. A high relevance score
alone can also over-select replicas, omit primary sources, or spend the download budget before
important source categories are represented.

## Decision

1. M06 accepts one confirmed `ScientificDataContract`, its exact integrity-valid M04 `SearchPlan`,
   and the exact integrity-valid M05 `ConnectorExecutionResult`. Task, run, version, contract hash,
   plan hash, and candidate-set references must all agree before selection starts.
2. Every report entry is explicitly candidate-only. Coverage is projected through the exact M04
   field/source cells and retains M05 assessment, confidence, basis, evidence IDs, source IDs, and
   contract source types. `unknown` claims and claims below the policy threshold contribute no
   coverage. M07 and later parsing stages must re-evaluate every field.
3. Selection is deterministic and budget-bounded. Replica groups contribute at most one candidate;
   each selected candidate contributes at most one assigned diversity category even if merged
   metadata lists multiple categories. Ranking prioritizes new Required-field coverage, uncertain
   Required-field evidence, a missing primary source, source types, categories, optional fields,
   open-license clarity, direct locators, and the fixed M05 assessment, with candidate ID as the
   final tie breaker.
4. Since M05 does not provide authoritative download sizes, each selected source reserves the
   policy's explicit unknown-size byte amount. No actual size is invented. Exhaustion becomes a
   structured blocking gap.
5. Coverage reports include the complete confirmed field, entity-key, quality-gate, selection-
   constraint, coverage-cell, and acceptable-source-type universes. Uncovered and uncertain
   Required fields, missing primary sources, insufficient diversity, unverified scopes, unresolved
   reuse permission, and budget exhaustion become structured gaps and deterministic search
   directives where another search can help.
6. License decisions are conservative. `allowed` requires exclusively open access metadata, a
   non-empty normalized license, and a full open-license assessment. Restricted-only metadata is
   `restricted`; everything else is `needs_review`. Selection never turns missing permission into
   permission.
7. The result retains the full progress snapshot. The existing deterministic stop policy evaluates
   hard limits first and permits coverage saturation only when there are no blocking gaps and the
   configured recent low-gain window is satisfied. M06 emits `selection.completed`; it does not
   emit `search.completed` while another round is required.
8. Input, selected-set, coverage-report, gap-set, and output hashes are canonical and independently
   recalculated. Stable artifact IDs derive from their semantic hashes. Identical requests return
   the same process-local cached result and stable event ID.

## Consequences

- Downstream download receives explicit reasons, evidence references, byte reservations, locator
  readiness, and license decisions without receiving invented scientific values.
- A fixture may achieve 100% candidate coverage and still be `partial` because record-level scope
  or redistribution permission remains unverified. This is intentional.
- The deterministic greedy policy is explainable and bounded, but it is not claimed to be globally
  optimal. Benchmark Recall@20, nDCG@20, and invalid-download rate require a versioned judged corpus
  and real download outcomes.
- Cache and event delivery are process-local. Durable artifact storage, distributed idempotency,
  downloader enforcement, and post-parse coverage updates remain later checkpoints.
