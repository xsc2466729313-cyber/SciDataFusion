# SciDataFusion

SciDataFusion is an evidence-first AI data scientist for the 2026 Challenge Cup Alibaba
Cloud topic, Track 2 / Direction 1A: scientific data discovery, parsing, and integration.
It turns a natural-language research goal into an analysis-ready, traceable, reproducible,
and correctable scientific dataset.

The source package is being built phase by phase from the
[V4 specification package](./%E9%9C%80%E6%B1%82%E5%88%86%E6%9E%90/AI_Data_Scientist_Codex_Documentation_V4/README.md).

## Current checkpoint

Phase 2 is complete and Phase 3 is in progress. Phase 1 turns an accepted research goal into a
confirmed scientific data contract; M04-M06 discover and select evidence-backed sources; M07
preserves authorized bytes in immutable Bronze storage; M08 creates an explainable,
registry-bound downstream parse plan; and M09 executes the eligible document routes into a
provenance-preserving unified document IR:

- M00 security, privacy, upload, and finite-budget intake gates;
- M01 evidence-grounded problem compilation with deterministic fallback and a validated Qwen
  provider boundary;
- M02 replayable domain/task routing with fail-closed runtime capabilities;
- M03 content-addressed Schema Packs, conflict-preserving field composition, JSON Schema, and
  immutable contract confirmation;
- an idempotent M00-M03 workflow with ordered causal checkpoints and a no-network Ia supernova
  demonstration command;
- M04 strict search contracts, evidence-grounded research concepts, deterministic query families,
  field/source coverage templates, budget allocation, and unit-testable stop decisions;
- a content-addressed source-capability registry whose declarations are kept separate from the
  runtime health snapshot. The runtime default supplies zero capabilities and fails closed;
- M05 strict Connector contracts, a content-addressed registry, fixed HTTPS endpoints, credential
  environment references, bounded HTTP execution with identity-only responses, cross-call circuit
  recovery, deterministic batch byte allocation, raw-page manifests, and structured run logs;
- VizieR TAP, OpenAlex, Zenodo, and Crossref-backed supplement adapters that are fully exercised
  with Mock transport, plus deterministic candidate normalization, provenance, conflict retention,
  parser-version-bound cache replay and origin tracking, initial coverage claims, and source
  assessment;
- an offline Ia acceptance fixture that executes eight planned queries over nine pages and reduces
  eight raw hits to five provenance-rich candidates with zero confirmed-live and zero
  unknown-network attempts;
- M06 strict candidate-coverage, selected-source, gap, progress, metric, and event contracts with
  canonical upstream/output integrity verification;
- deterministic replica-aware selection that balances Required/optional fields, primary sources,
  source categories/types, locator readiness, conservative license decisions, and explicit
  unknown-size byte reservations;
- a complete candidate-only report over contract fields, entity keys, quality gates, selection
  constraints, M04 cells, and source types, plus reproducible gap directives and stop decisions;
- M07 per-hop locator authorization, controlled HTTPS/DNS-pinned downloads, bounded retries and
  byte budgets, byte-based media inspection, safe ZIP extraction, and exact failure accounting;
- SHA-256 Bronze storage, cross-source deduplication, root/attachment/archive provenance,
  deterministic events, single-flight execution, and durable complete-result checkpoints;
- an offline Ia acquisition fixture with five unique Bronze objects, six provenance acquisitions,
  no external network, and a privacy-reduced `phase3-download-demo` summary;
- M08 strict classification, capability-registry, runtime, policy, page-scope, route, gap, metric,
  aggregate-plan, checkpoint, and `parse.plan.created` event contracts bound to the exact M07
  request, result, completion event, and Bronze hashes;
- signature-first bounded classification with conservative structural facts, low-cost-first parser
  selection, quality-gated OCR fallback planning, explicit unknown/capability gaps, and no raw-byte
  or scientific-value retention;
- a content-addressed parser registry separated from its runtime availability snapshot, plus
  immutable complete-result checkpoints and single-flight execution;
- an offline Ia M08 fixture result with five classifications and five plan entries: four executable
  routes, one archive metadata-only disposition, one conditional PDF OCR fallback, and zero
  network, model-classification, downstream-parser, or Bronze-write operations;
- M09 strict document request, runtime, parser-attempt, quality, candidate, comparison, gap,
  `DocumentIR`, `PageIR`, `BlockIR`, result, checkpoint, and `document.parsed` event contracts;
- deterministic local pypdf, HTML, and plain-text adapters that preserve source-byte lineage,
  retain exact source spans where available, label pypdf coordinates as approximate, and annotate
  repeated PDF headers and footers without discarding their text;
- content-addressed document IR storage, complete-result checkpoints, exact upstream and output
  integrity verification, single-flight execution, cancellation isolation, and quality-triggered
  fallback controls;
- an offline Ia M09 result with three eligible document routes, two successful document IRs, one
  honest PDF review outcome, a blocked unavailable OCR fallback, and zero network, model, M10,
  M11, M13, or Bronze-write operations.

M04 does not access the network. M05 implements a live-capable but default-offline transport
boundary; repository acceptance uses only offline fixtures and Mock transport with no real API
credentials or external-source calls. M06 adds no network or model call. In the Ia fixture it
selects three source categories with full Required/entity/source-type candidate coverage, then
correctly remains partial because record-level scope and reuse permission are not yet proven. M07
retains controlled source bytes but does not parse scientific values. M08 classifies and plans only;
M09 parses supported documents into document IR but does not extract scientific fields. M10 table
IR, later evidence/normalization/fusion, and the web workbench remain future checkpoints.

## Quick start

```powershell
uv sync --python 3.11 --group dev
Copy-Item .env.example .env
uv run scidatafusion doctor
uv run scidatafusion phase1-demo --goal "Integrate multi-source Type Ia supernova light curves into CSV." --confirmed-by "demo-reviewer"
uv run scidatafusion phase2-plan-demo --goal "Study Type Ia supernova light curves using multi-source data integration into CSV." --confirmed-by "demo-reviewer"
uv run scidatafusion phase2-connect-demo --goal "Study Type Ia supernova light curves using multi-source data integration into CSV." --confirmed-by "demo-reviewer"
uv run scidatafusion phase2-select-demo --goal "Study Type Ia supernova light curves using multi-source data integration into CSV." --confirmed-by "demo-reviewer"
uv run scidatafusion phase3-download-demo --goal "Study Type Ia supernova light curves using multi-source data integration into CSV." --confirmed-by "demo-reviewer"
uv run scidatafusion phase3-parse-plan-demo --goal "Study Type Ia supernova light curves using multi-source data integration into CSV." --confirmed-by "demo-reviewer"
uv run scidatafusion phase3-document-demo --goal "Study Type Ia supernova light curves using multi-source data integration into CSV." --confirmed-by "demo-reviewer"
uv run pytest tests/test_search_planning.py -q --no-cov
uv run pytest tests/test_connector_contracts.py tests/test_connector_registry.py tests/test_connector_normalizer.py tests/test_connector_http.py tests/test_connector_execution.py -q --no-cov
uv run pytest tests/test_selection_contracts.py tests/test_selection_integrity.py -q --no-cov
uv run powershell -ExecutionPolicy Bypass -File scripts/check.ps1
```

`doctor` never prints secret values. The default configuration is offline and does not call
Alibaba Cloud or any external data source. `phase1-demo` also stays offline and labels its
capability snapshot as `simulated_demo`; it is an engineering demonstration, not a production
Connector health claim. `phase2-plan-demo` similarly selects `simulated_demo` explicitly for its
fixture capabilities; loading either static registry alone never marks a source healthy. M05 tests
and `phase2-connect-demo` inject `offline_fixture` or Mock runtime state and never treat those
results as live-source proof. The Connector demo drives the real four adapter parsers using
packaged response bytes; its summary omits research text, reviewer identity, candidate content,
URLs, and untrusted excerpts.

`phase2-select-demo` carries the same offline proof through M06. It exposes only opaque candidate
IDs, aggregate candidate coverage, reason codes, license/readiness states, gap codes, and immutable
hashes. `candidate_covered` is a discovery claim, not parsed-field or scientific-value proof. M07
retains immutable bytes; M08 and later parsing stages must re-evaluate coverage from those bytes.

`phase3-download-demo` drives the same M07 contracts, downloader, archive inspection, Bronze store,
manifest, run log, and event construction used by the service. Its output omits URLs, filenames,
approval references, source content, the research goal, and reviewer identity. Acceptance is
offline only: it does not claim real-source availability, legal approval, mid-download resume, M00
upload ingestion, or parsed Silver/Gold data.

`phase3-parse-plan-demo` carries the exact M07 request, result, completion event, and in-memory
Bronze bytes into M08. Its privacy-reduced output reports format families, route dispositions,
downstream module families, parser identifiers, costs, gaps, and hashes. M08 performs no parser,
OCR, VLM, network, or model call and writes no Bronze bytes. The packaged Ia PDF is intentionally
minimal, so this checkpoint does not claim the specification's file/page accuracy or latency
targets; those require a versioned judged routing corpus and representative mixed-page PDFs.

`phase3-document-demo` carries that exact chain into M09 and executes only `m09.pdf_text`,
`m09.html`, and `m09.text`. The HTML and text artifacts produce validated content-addressed IR;
the page-less packaged PDF fails honestly and its unavailable OCR fallback is recorded as blocked,
so the aggregate result is `partial`. The summary contains only hashes, statuses, parser IDs,
quality counts, and aggregate metrics. It exposes no document body, URL, reviewer identity, or
scientific value and performs no network or model call. A separate valid two-page PDF fixture
checks local pypdf extraction, approximate normalized coordinates, and non-destructive repeated
header/footer annotation. This slice does not establish the representative-corpus fidelity,
reading-order, heading, bbox, failure-rate, or latency targets.

Connector attempts expose tri-state network audit: `true` is confirmed live, `false` is confirmed
not performed, and `null` is unknown after an unexpected live failure. Unknown attempts are reported
separately and never counted as confirmed live. Credential values are used only to construct the
outbound authentication request; M05 does not proactively persist them in application artifacts or
logs. Responses reflecting a credential in common direct or encoded forms are quarantined before
hashing or storage, but this is a bounded safeguard rather than an absolute leak-detection
guarantee.

## Quality commands

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
uv run bandit -c pyproject.toml -r src
uv run python scripts/scan_secrets.py
```

The official competition requires the final application to use Qwen through Alibaba Cloud Model
Studio or an approved competition tool. The regional provider boundary, timeout/retry/cache
controls, strict output validation, and audit record are implemented and Mock-tested. No real
credential was used in repository tests; a credentialed competition-environment proof remains a
separate deployment acceptance item.
