# Implementation roadmap

The V4 specification is implemented as independently accepted checkpoints.

| Phase | Scope | Demonstrable exit | Status |
|---:|---|---|---|
| 0 | repository, contracts, configuration, quality gates | locked clean build and doctor | complete |
| 1 | M00-M03 | research goal to confirmed data contract | complete |
| 2 | M04-M06 | federated discovery, coverage, selected sources | complete |
| 3 | M07-M10 | immutable download, document and table IR | complete (first offline vertical slice) |
| 4 | M13-M15 | field evidence, mapping, unit/time normalization | complete (first offline vertical slice) |
| 5 | M16-M18 | entity resolution, conflict-preserving fusion, repair/HITL | complete (first offline vertical slice) |
| 6 | M19 | hybrid retrieval and evidence graph | complete (first offline vertical slice) |
| 7 | M11-M12 | chart digitization and scientific formats | complete (first offline vertical slice) |
| 8 | M20 | FastAPI, interactive workbench, exports, reproduction bundle | complete (first offline vertical slice) |
| 9 | evaluation | three domains, held-out domain, ablations, demo package | pending |

## Vertical-slice order

After Phase 1, the first deep case is Ia supernova light curves. M04 emits a bounded source plan
covering VizieR `tap_adql_discovery`, literature metadata, a data repository, and supplement/web
discovery. M05 binds those queries to fixed, allowlisted HTTPS Connectors and produces normalized,
provenance-rich source candidates with initial deterministic assessment. Its acceptance evidence is
offline/Mock only: eight queries, nine pages, eight raw hits, five candidates, and zero live-network
or unknown-network attempts. It does not claim real source availability or selection.

M06 measures candidate-only Required-field, entity-key, scope, primary-source, quality-gate, and
source-type coverage; selects an explainable download set under byte-reservation and diversity
constraints; reports remaining gaps; and retains the progress used for a reproducible continue or
stop decision. The Ia fixture selects three categories with full candidate field/type coverage but
remains partial because record-level scope and reuse permission are unverified. The same contracts
then support materials/chemistry and environment/life-science cases.

M07 converts authorized selected-source locators into content-addressed Bronze bytes under exact
HTTPS, redirect, DNS, retry, rate, byte-budget, MIME, archive, and license controls. The offline Ia
fixture produces five unique objects and six provenance acquisitions with no external network, and
complete results can replay from an immutable checkpoint. M08 consumes those objects without
changing them; M07 does not claim parsed scientific values or Silver/Gold completeness.

M08 verifies the exact M07 request/result and Bronze hashes, classifies each unique object from
bounded inert samples, and creates one content-addressed aggregate plan from a separately verified
parser registry and runtime snapshot. The offline Ia slice accounts for all five objects with four
executable routes and one archive metadata-only disposition; one PDF OCR fallback is planned but
not executed. M08 does not claim parser success, recovered scientific values, or benchmark-backed
page-routing accuracy.

M09 verifies the exact M07-M08 chain and executes only its three eligible document routes through
bounded local pypdf, HTML, and plain-text adapters. It produces two content-addressed document IRs
and keeps the malformed page-less PDF as an explicit review outcome with an unavailable OCR
fallback, so the offline Ia aggregate is partial without inventing content. Quality decisions,
candidate selection, gaps, parser/runtime identities, checkpoint replay, and one privacy-reduced
completion event remain independently verifiable. M09 does not extract scientific fields or
establish representative-corpus accuracy targets.

M10 verifies the same M07-M08 chain and executes its one native CSV route. The first accepted slice
produces a two-row, four-column TableIR with eight exact byte-evidenced cells, deterministic quality
gates, content-addressed replay, and all-String Polars projection. It does not silently repair a
header or coerce scientific-looking text. XLSX, HTML/PDF/image tables, complex headers, merged
cells, footnotes, and cross-page reconstruction remain explicit format-capability gaps rather than
claims of this slice. M10 does not claim field extraction or Gold correctness.

M13 verifies the exact confirmed contract and M10 lineage, then creates only explicit candidates
whose headers exactly equal contract fields. Every candidate references a minimal table-cell
EvidenceAtom that replays to immutable Bronze bytes and same-row evidence for every entity key. The
Ia fixture yields four evidence-bound candidates and remains partial because one required field is
absent. It performs no semantic alias mapping, inference, derivation, normalization, model or
network execution, or Gold write.

M14 re-verifies the exact M13 request/result and maps every existing candidate to the same canonical
contract field only after contract-hash, type-label, value-evidence, entity-evidence, score, and
threshold checks. The Ia fixture accepts four mappings for M15 and retains the missing-required
upstream gap, so it remains partial. Unknown source headers are preserved with exact header-cell
lineage and registered-alias suggestions, but cannot auto-map without M13 value evidence. This
first slice performs no embedding, LLM, network, value transformation, or Gold write.

M15 re-verifies M13-M14 and retains every mapped field. It parses finite numeric lexemes through
exact decimal arithmetic and records every non-identity change with formula, library version,
precision metadata, reversibility, and evidence. Because the Ia source cells do not evidence source
units or a time scale, M15 does not treat target units as source context and does not claim an MJD
or magnitude conversion. It emits three blocking issues, keeps two identity fields eligible for
M16, and remains partial without model, network, guessing, Gold writes, or binary-float coercion.

M16 re-verifies M15 and buckets records by exact evidence-backed contract entity-key fingerprints.
Only multi-record exact-key buckets can auto-merge, at score and threshold 1.0; exact duplicates
also require equality across every M16-eligible field. The one-record Ia fixture therefore yields
one evidenced singleton cluster, zero candidate pairs, zero automatic merges, and zero duplicate
groups. It inherits the upstream partial status, exposes no entity values, and performs no fuzzy,
LLM, network, Gold, or scientific-value mutation operation. M17 next owns conflict-preserving
fusion within those clusters.

M17 re-verifies the exact M16 chain and converts every normalized field in each eligible entity
cluster into an immutable fusion candidate. A single candidate or multi-candidate exact consensus
is selectable only when every candidate is M16-eligible and evidence-backed. All other fields are
withheld, and distinct candidate hashes create an unresolved conflict with no selected value. The
one-record Ia fixture retains four candidates, selects two identity fields into a Gold candidate
view, withholds two context-blocked fields, and produces zero conflicts or silent overwrites. It
remains partial with no tolerance aggregation, source-priority rule, uncertainty aggregation, LLM,
network, final Gold publication, or benchmark accuracy claim. M18 next owns quality audit, repair,
and human review.

M18 re-verifies the exact M17 chain and evaluates every registered contract quality gate against
the Gold candidate records. Failed record-level gates become evidence-referenced issues with
derived severity, whitelist actions, bounded repair impact, and one pending review item each. The
Ia candidate fails required-field completeness, required-field provenance, and the photometric
any-of gate, so the result contains three Critical issues and no formal Gold. No repair executes,
the before/after score remains unchanged, and no scientific value is mutated. Domain validators,
statistical anomaly benchmarks, reviewer resolution, local retry execution, and approved export
remain future slices. Phase 6 next begins M19 hybrid retrieval and evidence graph work.

M19 re-verifies the exact M18 chain and projects M13 EvidenceAtoms plus M18 quality gates and issues
into ten task-private index documents. A `rank-bm25` sparse channel runs after task/permission
filtering, while one-hop graph expansion contributes a separate score. Every retrieval hit retains
source identity, location, index version, and channel scores. The evidence graph contains eighteen
nodes and thirty-three edges and participates in evidence-lineage validation, retrieval expansion,
and memory admission. Since M18 produced no formal Gold, the sole task memory is quarantined and
non-reusable; immutable revocation creates a successor that preserves the prior memory hash. Dense
embedding, Qwen reranking, cross-task retrieval, document-block indexing, and benchmark metrics are
deferred. Phase 7 next begins M11 chart digitization and M12 scientific-format parsing.

M11 accepts a content-addressed direct figure plus explicitly confirmed two-tick calibrations. Its
bounded P6 PPM adapter extracts exact-color connected components and uses Decimal transforms for
linear/log10 and increasing/decreasing pixel directions. The synthetic Ia acceptance raster yields
three points; every point preserves its component bbox, centroid, calibrated coordinates, error,
both CalibrationRecord identities, and source-byte hash. The result remains partial because axis
and series semantics are manual inputs. OCR, VLM, automatic legend/axis recognition, overlapping
series, M08/M09 region routing, and benchmark accuracy are deferred.

M12 accepts a content-addressed FITS scientific artifact plus an explicit HDU, variable, and row
subset. Its optional Astropy plugin reads one binary table deterministically and emits a unified
DatasetIR with source row coordinates, variables, original storage values, decoded physical
values, FITS column formats and units, bounded header cards, TSCAL/TZERO transformations, and
preserved non-finite missing values. The synthetic Ia fixture contains three variables and four
rows; all twelve selected cells replay exactly, including one scaled magnitude variable and one
missing uncertainty. Content-addressed DatasetIR storage, canonical checkpoints, plugin/runtime
binding, Bronze replay, and one `dataset.parsed` event are independently verified. NetCDF, HDF5,
GeoTIFF, FASTA, large-file Dask execution, and a full M07-M08 FITS acquisition fixture remain
additional adapters rather than claims of this slice.

M20 re-verifies the exact M19 chain and creates only quality-gated delivery artifacts. Formal Gold
drives exact-equivalent CSV/Parquet serialization; the current Ia result has no Formal Gold, so the
workbench and CLI withhold those files and expose three review items. The deterministic ZIP retains
a canonical file manifest, hashes, contract dictionary, provenance, quality state, evidence graph,
run/runtime metadata, and an executable hash-verification notebook. FastAPI provides a usable
input/status/download loop with short-lived content-bound HMAC tickets. The first slice uses local
stores and an ephemeral signing key; production persistence, identity integration, live sources,
and successful reviewed Gold publication remain future deployment/evaluation work. The product UI
projects the whole workflow into Chinese business views rather than exposing module checkpoints:
research progress, sources and artifacts, parsing/integration details, evidence and quality, and
delivery. Its light-curve and evidence-graph canvases use actual M11/M19 outputs.

M21 adds an optional online-discovery branch in front of the unchanged deterministic parsing and
integration chain. SerpApi is the only live search endpoint and is guarded by a host allowlist,
timeout, retry budget, concurrency/rate limits, cache, bounded result count, and secret-free hashes.
The returned title, URL, domain, and snippet are validated as untrusted input. Qwen then receives
only the research goal and bounded search summaries and may return strict source relevance,
evidence-type, rationale, and action candidates. Any extra field, duplicate/unknown URL, invalid
JSON, or provider failure discards the assessment while retaining the validated search evidence.
Neither branch can create, repair, select, or mutate a scientific value. The Chinese workbench now
exposes explicit offline/online modes, runtime readiness, live results, provider proof, and honest
degraded states. Repository acceptance uses Mock providers; real keys remain local and ignored.

M22 turns that one-query branch into a configurable research strategy. Operators can choose Google
or Google Scholar, locale, optional country, automatic Qwen planning, one to four queries, and the
retained-result bound. Qwen planning uses its own versioned prompt and strict schema; the user's seed
query is always retained, duplicate proposals are removed, and each planned query receives an
ordered success/failure record. Partial failures preserve successful results, which are URL-deduped
before the separate source assessment call. A secret-free configuration API and Chinese
`联网配置` view expose the effective provider, endpoint host, models, strategy, limits, and credential
readiness. Endpoint allowlists and the prohibition on model-created scientific values remain
unchanged.

M23 replaces the single-engine online boundary with three explicit search channels. Qwen assigns
each bounded query to Google Web, Google Scholar, or arXiv; deterministic fallback planning covers
all three. SerpApi serves the two Google channels, while the public arXiv Atom API requires no new
credential and is protected by an exact host allowlist, three-second request spacing, retry, cache,
response-size limit, and hardened XML parsing. Results are merged round-robin across channels and
deduplicated by URL, with a default retained-source limit of 20. The workbench exposes the selected
channel and immutable invocation proof for every query and source.

M24 closes the online discovery-to-material gap with bounded automatic acquisition. Up to five
validated search-result URLs explicitly recommended for download are admitted to a request-scoped
exact-host allowlist, DNS-pinned to public addresses, fetched without redirects or credentials,
classified from bytes, and stored content-addressed in Bronze. Per-file/run byte budgets, timeout,
rate limit, and structured failures keep cost and risk bounded; no downloaded material can populate
a scientific field before deterministic parsing and evidence binding.

M25 makes acquired current-topic objects directly downloadable by their verified SHA-256 and adds a
checkpointed Agent reflection loop. The loop repeatedly searches, acquires, measures material gaps,
and changes retrieval route for up to four rounds. Success requires three unique artifacts across
two domains and at least one machine-readable table, scientific file, or data archive; HTML landing
pages and PDF documents remain evidence but cannot satisfy that data target. Every reflection round
and model proof is stored immutably in DuckDB, while an unmet target remains an explicit resumable
checkpoint rather than a false success or fabricated Gold result.

M26 turns the local workflow into a deployable AI research platform without changing its scientific
trust boundary. A strict asynchronous job API has interchangeable in-memory and PostgreSQL
repositories; Celery and Redis isolate worker execution, while Chroma indexes only evidence-derived
documents. A bounded LangGraph runner may expose LangChain and LlamaIndex views, scikit-learn hashing,
and optional PyTorch validation, but none of those components can create or mutate a scientific
value. The Chinese React/TypeScript/Vite workbench is delivered through Docker Compose and embedded
in the Windows package, with adaptive research planning, source/evidence views, and an interactive 3D
knowledge graph. Local mode remains the zero-infrastructure reproducible profile.

Each phase ends with contract tests, offline replay fixtures, metrics, security checks, an ADR for
new architectural choices, and an updated acceptance record.
