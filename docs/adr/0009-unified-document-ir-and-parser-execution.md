# ADR-0009: Unified document IR and quality-gated parser execution

## Status

Accepted and implemented for the first M09 offline slice. The acceptance evidence and explicit
benchmark boundaries are recorded in `docs/phase-3-m09-acceptance.md`.

## Context

M08 creates an immutable, content-addressed parse plan from the exact M07 Bronze snapshot. It does
not execute the planned parser, establish document fidelity, or emit document, page, or block IR.
M09 must execute only the M09 routes in that plan while retaining the source-byte identity,
upstream lineage, parser identity, quality decisions, and every unresolved limitation.

Parser libraries expose incompatible private objects and location models. A PDF has physical pages
and page coordinates, while HTML and plain text are flow documents whose trustworthy origin is a
source span rather than an invented page. PDF text coordinates also vary in reliability: simple
text placement can be recovered deterministically, but transformation matrices, nested forms,
rotated text, and complex layouts can make derived coordinates approximate.

The first offline slice needs a credential-free default PDF parser. It must not silently introduce
a strong copyleft runtime obligation or pretend that a lightweight parser provides OCR, semantic
layout recovery, or exact coordinates for every PDF. The packaged Ia fixture also contains a
minimal PDF catalog with no recoverable page or body text, so a successful M08 route for that file
cannot be treated as successful M09 parsing.

## Decision

1. M09 accepts the exact integrity-valid M08 request, result, completion event, and parse plan. It
   resolves every planned source against the same read-only M07 Bronze object, manifest metadata,
   byte hash, and acquisition lineage. M09 never writes, replaces, annotates, or repairs Bronze.
2. M09 executes only routes whose target module is M09. CSV/table routes remain owned by M10,
   chart digitization remains owned by M11, and scientific field candidates and `EvidenceAtom`
   creation remain owned by M13.
3. Parser outputs are normalized into strict, versioned `DocumentIR`, `PageIR`, and `BlockIR`
   contracts. No parser-library private object crosses the module boundary. Each normalized object
   retains its source object and byte hash, parser ID and actual version, raw text, block kind,
   reading order, confidence, and location provenance.
4. Locations are source-type aware. Paginated sources use a page reference, declared coordinate
   space, page dimensions, and a validated bounding box when one can be supported. Flow documents
   use a source span, such as byte or character offsets and an optional DOM path. M09 never invents
   a page or bounding box for HTML or plain text merely to make contracts look uniform.
5. `pypdf` 6.14.2, distributed under the BSD-3-Clause license, is the default PDF text parser for the
   first slice and is locked to the 6.x compatibility range. PyMuPDF is not included because this
   checkpoint does not accept its AGPL/commercial licensing choice. Coordinates derived by pypdf
   from complex transformation matrices are marked `approximate`; an exact-coordinate gate fails
   closed when the parser cannot support it.
6. HTML and plain-text parsing is deterministic and local. It consumes only the verified Bronze
   bytes, does not execute scripts or document instructions, does not load remote styles, images,
   frames, or other assets, and does not follow links. External-document instructions remain data.
7. Every parser attempt produces an immutable candidate record. A primary result, fallback result,
   or alternate-page result cannot overwrite another candidate. Selection occurs at the narrowest
   supported document, page, or block scope and records the selected candidate, rejected
   candidates, scores, conflicts, and deterministic reason.
8. Fallback is driven only by the quality-check failures named in the M08 route and permitted by
   the current execution policy, budget, and runtime capability snapshot. Initial deterministic
   gates cover output schema, source lineage, page integrity, text coverage, bounding-box validity,
   and reading order. M09 does not run every parser speculatively or reinterpret M08's global
   routing decision.
9. A parser crash, malformed output, unsupported feature, missing text layer, unusable coordinate
   result, or unavailable guarded fallback becomes a structured failure or review outcome. The
   packaged Ia PDF must end in document-level `needs_review`; if its HTML and text routes remain
   valid, the aggregate module result may be `partial`, but no PDF body, page, or coordinate is
   fabricated.
10. Real OCR/VLM execution, Docling, MinerU, GROBID, other heavyweight parsers, and the 200-page
    representative benchmark are outside the first offline slice. Their capability records must
    remain unavailable in the M09 execution snapshot until real adapters, policy controls, and mock
    and fault tests exist. A fake adapter may test a contract but cannot establish runtime health.
    The pypdf adapter applies the M09 16 MB content-stream policy after pypdf has decoded the
    stream. pypdf 6.14.2 independently caps supported decompression and array-filter output at 75
    MB, so allocation is bounded by that library guard but not by the lower per-call policy before
    decode. A process-isolated or streaming decoder is required before this baseline can claim the
    stronger pre-allocation bound.
11. Normalized candidates, selected IR, comparison reports, and the aggregate result are
    content-addressed. The M09 input hash binds exact upstream hashes, route and registry hashes,
    Bronze byte and metadata hashes, parser/runtime/policy versions, contract version, and producer
    version. Identical input replays an immutable checkpoint; `force_recompute` creates an explicit
    attempt without silently replacing an accepted result.
12. M09 emits the existing canonical `document.parsed` event, not the conflicting illustrative
    name `m09.completed`. One aggregate event contains only references, hashes, status, counts,
    quality-gate outcomes, and the idempotency key. It contains no document body, sensitive source
    metadata, credentials, scientific values, or parser-private output.

## Consequences

- M10, M11, and M13 receive stable, parser-independent locations and text without depending on a
  PDF or HTML library's private data structures.
- A paginated block can be traced back to its page and supported coordinate precision; a flow
  block can be traced back to its original source span without fictitious page geometry.
- The permissively licensed local default is reproducible and network-free, but complex PDF
  layout, scans, formulas, and exact coordinates will more often produce explicit review outcomes
  until separately governed parsers are available.
- The existing Ia vertical slice is expected to demonstrate honest partial delivery and review,
  not PDF parsing success or OCR quality. A separate valid text-bearing PDF fixture is required to
  test the deterministic PDF golden path without mutating the existing Bronze fixture.
- A malformed or page-less PDF produces no `DocumentIR`, page, block, quality candidate, or
  comparison. Its primary adapter failure is retained as an executed attempt; unavailable OCR is
  retained as a blocked, non-executed attempt with no fabricated engine identity. Only an actually
  executed fallback requires, and can claim, the exact failed quality-gate trigger from M08.
- Candidate retention and content-addressed checkpoints require more storage than keeping only a
  selected parse, but make comparison, replay, audit, and local rerun possible without scientific
  value overwrite.
- M09 may locate table and figure regions and preserve captions or source references. It does not
  reconstruct cells, digitize plotted points, extract scientific fields, create evidence atoms, or
  claim Silver or Gold correctness.
