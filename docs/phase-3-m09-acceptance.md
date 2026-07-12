# M09 acceptance: unified document parsing and IR

## Status

Accepted on 2026-07-12 for the first M09 offline slice described below. Representative-corpus
accuracy, latency, native-parser isolation, and real OCR/VLM execution remain explicitly unproven.

## Definition of Done

- [x] Strict Pydantic v2 contracts with `extra="forbid"` cover the M09 request, parser attempts,
  parser candidates, source-aware locations, `DocumentIR`, `PageIR`, `BlockIR`, parser comparison,
  quality gates, gaps, metrics, aggregate result, and `document.parsed` event. Invalid enums,
  missing fields, non-finite numbers, unbounded content, and incompatible contract versions fail
  with structured errors.
- [x] M09 verifies the exact M08 request, result, completion event, plan, registry and runtime
  snapshots, M07 artifact set and manifest, Bronze metadata, acquisition lineage, and bytes before
  invoking a parser. Tampering or post-verification byte drift fails closed.
- [x] Exactly the M08 routes targeting M09 are executed. M10 table routes, archive metadata-only
  entries, M11 figure work, M12 scientific formats, and M13 extraction are not executed by M09.
- [x] One selected parse is normalized into parser-independent IR per successful document. PDF
  blocks retain page, coordinate space, coordinate precision, and supported bbox; HTML and text
  blocks retain source spans and do not receive fabricated page geometry.
- [x] `pypdf` 6.14 is the bounded, BSD-licensed PDF text default. A valid text-bearing PDF golden
  fixture proves local page/text normalization, while complex or unsupported coordinates remain
  explicitly `approximate` or fail the relevant quality gate.
- [x] HTML and plain-text parsers operate deterministically over verified bytes without network,
  script execution, remote asset loading, link traversal, document-instruction execution, or
  scientific-value transformation.
- [x] Every parser attempt and normalized candidate is retained by content hash. Selection records
  all candidates, conflicts, gate scores, the selected candidate, and the deterministic reason;
  repeated headers or footers are suppressible annotations rather than destructive loss of the
  original candidate text.
- [x] Output-schema, source-lineage, page-integrity, text-coverage, bbox-validity, and reading-order
  gates are computed in code. Only a failed gate named by the M08 route may trigger an ordered,
  available, policy- and budget-permitted fallback.
- [x] Real OCR/VLM, Docling, MinerU, GROBID, and other deferred parsers are unavailable in the
  execution capability snapshot. Fake adapters are limited to contract, fallback, timeout, and
  malformed-output tests and are never reported as real parser success.
- [x] The current Ia PDF yields a structured document `needs_review` result because it has no
  recoverable page or body text. M09 does not invent a page, bbox, text, parser success, or OCR
  result. Valid HTML and text results remain deliverable through aggregate `partial` status.
- [x] Input, candidate, comparison, IR, result, event, idempotency, and output hashes are
  independently verified. Identical calls replay one immutable checkpoint, concurrent callers
  share one shielded execution, cancellation is isolated, and `force_recompute` is explicit.
- [x] Exactly one privacy-reduced `document.parsed` completion event is emitted after the complete
  result passes integrity checks. Checkpoint replay does not emit a duplicate event or re-invoke a
  parser.
- [x] Parser exceptions, malformed output, unavailable fallback, cancellation, budget exhaustion,
  storage half-write, checkpoint tampering, and unsupported documents have typed failure or review
  outcomes. No failure path silently promotes incomplete IR.
- [x] Metrics and warnings are derived from immutable execution records rather than hand-entered.
  They include route and outcome counts, page, block and text counts, attempts, fallback decisions,
  quality-gate results, parser identity/version, costs, and block reasons without document bodies,
  secrets, sensitive URLs, or authorization data. Latency, retry, and cache-hit metrics are deferred
  rather than inferred from nondeterministic wall-clock state.
- [x] Public APIs have complete typing and docstrings; an ADR and module acceptance document state
  the coordinate, parser, licensing, event, idempotency, and downstream boundaries.
- [x] Focused tests and the full repository pass Ruff, format checking, strict mypy, pytest with
  coverage, Bandit, secret scanning, and dependency auditing before this checkpoint changes from
  `pending` to accepted.

## Offline Ia acceptance evidence

The M09 demo consumes the exact packaged M00-M08 Ia chain and its read-only Bronze objects. The
implemented acceptance assertions are:

- the five M08 entries remain unchanged: PDF, HTML, plain text, CSV, and archive;
- exactly three M09 document routes reach parser execution: `m09.pdf_text`, `m09.html`, and
  `m09.text`;
- the M10 CSV route and archive metadata-only entry cause zero M09 parser calls;
- the HTML and plain-text routes produce validated IR from their immutable bytes;
- the malformed minimal PDF primary fails structurally and produces no fabricated page, text,
  candidate, quality score, or comparison; the route remains an explicit `needs_review` result;
- the guarded PDF OCR fallback is recorded as blocked and unavailable, with no engine identity,
  model call, network call, or parser invocation in offline mode;
- the aggregate M09 status is `partial`, with two deliverable document IR results and one explicit
  review outcome;
- external-network calls, model calls, scientific-field candidates, `EvidenceAtom` objects, M10,
  M11, and M13 executions, and Bronze writes all remain zero;
- repeated and concurrent identical requests replay the same immutable result and one completion
  event, while an explicit force attempt remains auditable;
- emitted summaries and the `document.parsed` event expose hashes, counts, status, gates, and gaps,
  but no raw document body, sensitive source metadata, or scientific values.

A separate, deterministic, text-bearing PDF fixture will verify the pypdf golden path, page and
block normalization, supported coordinates, source-byte lineage, replay, and content-addressed
output. It must not replace or mutate the existing Ia Bronze PDF simply to turn the planned review
outcome into success.

## Quality metrics

The immutable result calculates these deterministic runtime measures:

- route, attempt, candidate, selected-document, page, block, review, unsupported, and failure
  counts;
- input and output identity hashes;
- per-document text coverage, page-integrity, bbox-validity, and reading-order gate results;
- primary and fallback attempts, unavailable fallback decisions, costs, and parser versions;
- privacy-reduced event payload counts and downstream-module boundaries.

The offline Ia demo currently records three eligible routes, two successful routes, one review
route, four attempts, one blocked fallback, two candidates, two document IRs, two pages, two
blocks, 37 retained text characters, two gaps, zero model attempts, zero network attempts, and zero
cost. Its one `document.parsed` event reports only immutable identifiers, hashes, counts, and
status. Exact/approximate location distribution is retained in IR and tested, but is not duplicated
in the aggregate metrics contract.

The requirements' benchmark targets for content fidelity, reading-order accuracy, heading-hierarchy
F1, bbox validity, and supported-format failure rate require labeled representative data, a stated
baseline, sample size, and confidence intervals. The offline Ia fixture and one valid-PDF golden
fixture do not establish those targets. No benchmark value will be added here until the benchmark
is executed and its evidence is retained.

## Verification

Focused document-contract, adapter, integrity, checkpoint, service, fixture, and CLI tests are
included. Final acceptance uses the complete repository gate:

```powershell
uv run pytest tests/test_document_contracts.py tests/test_document_adapters.py `
  tests/test_document_service.py tests/test_cli.py -q --no-cov
uv run scidatafusion phase3-document-demo `
  --goal "Study Type Ia supernova light curves" --confirmed-by "m09-acceptance"
powershell -ExecutionPolicy Bypass -File scripts/check.ps1
```

The complete worktree gate passed on 2026-07-12 with 511 tests and 90.12% total coverage. Ruff,
format checking, strict mypy over 137 source files, Bandit, the repository secret scanner over 526
files, and dependency auditing all passed; the audit reported no known vulnerabilities. The Ia
demo also exited successfully with aggregate `partial` status and the measured metrics above.

## Known boundaries

- The packaged Ia PDF has no recoverable page or text body. Its review result tests honest
  degradation, not successful PDF extraction, OCR, content fidelity, or coordinate accuracy.
- pypdf is a deterministic text-layer baseline, not an OCR or semantic-layout engine. Rotated,
  nested, scanned, formula-heavy, encrypted, damaged, or otherwise complex PDFs may have
  approximate locations or explicit review outcomes.
- M09's 16 MB PDF content-stream policy is checked after pypdf decoding. pypdf 6.14.2 provides a
  separate 75 MB decompression/array-filter output guard, but this first slice does not isolate the
  native parser in a memory-limited worker or provide a streaming pre-allocation limit.
- Real OCR/VLM, Docling, MinerU, GROBID, other heavyweight parser execution, live parser health,
  and external parser credentials are deferred. Their execution capabilities remain unavailable.
- The 200-page representative parsing benchmark is deferred from the first offline slice, so the
  engineering targets for fidelity, reading order, headings, bbox validity, failure rate, and P95
  latency remain unproven.
- M09 may preserve table and figure region references, captions, and source locations, but does not
  reconstruct table cells, digitize chart points, parse scientific binary formats, extract fields,
  create `EvidenceAtom`, or claim Silver or Gold correctness.
- The first checkpoint proves local contracts, deterministic parser execution, honest quality
  gating, replay, and module boundaries. It does not prove production object-store durability,
  native-parser process isolation, arbitrary document support, or scientific correctness.
