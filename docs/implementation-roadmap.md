# Implementation roadmap

The V4 specification is implemented as independently accepted checkpoints.

| Phase | Scope | Demonstrable exit | Status |
|---:|---|---|---|
| 0 | repository, contracts, configuration, quality gates | locked clean build and doctor | complete |
| 1 | M00-M03 | research goal to confirmed data contract | complete |
| 2 | M04-M06 | federated discovery, coverage, selected sources | complete |
| 3 | M07-M10 | immutable download, document and table IR | complete (first offline vertical slice) |
| 4 | M13-M15 | field evidence, mapping, unit/time normalization | in progress (M13 complete) |
| 5 | M16-M18 | entity resolution, conflict-preserving fusion, repair/HITL | pending |
| 6 | M19 | hybrid retrieval and evidence graph | pending |
| 7 | M11-M12 | chart digitization and scientific formats | pending |
| 8 | M20 | FastAPI, interactive workbench, exports, reproduction bundle | pending |
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
network execution, or Gold write. M14 next owns registered field mapping and ambiguity handling.

Each phase ends with contract tests, offline replay fixtures, metrics, security checks, an ADR for
new architectural choices, and an updated acceptance record.
