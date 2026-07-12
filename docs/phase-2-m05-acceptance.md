# M05 acceptance: controlled federated Connectors and source assessment

## Exit criteria

- M05 accepts only an integrity-valid M04 `SearchPlan`, its exact capability registry, a
  content-addressed Connector registry, a matching runtime snapshot, and a finite execution
  policy. Registry, descriptor, plan, runtime-mode, and record-hash tampering fail closed.
- A source-neutral `Connector` protocol and batch executor isolate adapter behavior from
  orchestration. VizieR TAP, OpenAlex, Zenodo, and Crossref-backed supplement discovery are added
  through registered implementations without source-specific branches in the executor.
- The Connector registry pins one exact HTTPS endpoint and explicit public-host allowlist per
  source. Redirects are not followed, result URLs remain untrusted metadata, and adapters cannot
  replace the registered host, path, method, operation, dialect, or parser.
- Registry authentication contains references only. OpenAlex refers to `OPENALEX_API_KEY`, Zenodo
  refers to `ZENODO_ACCESS_TOKEN`, and resolved values are injected only into the outbound
  authentication field. M05 does not proactively persist those request values in contracts,
  request hashes, cache keys, application logs, artifacts, events, or committed configuration.
  Missing credentials produce structured, non-network failures when the selected execution mode
  requires them.
- Before accepting a response from an authenticated request, the transport checks for direct and
  common encoded reflections of the supplied credential and quarantines a detected response before
  raw-response hashing, artifact storage, parsing, or caching. This is a bounded defense against
  common reflection forms, not a guarantee that every possible transformation or side channel can
  be detected.
- The HTTP boundary applies connect/read/write/pool timeouts, bounded attempts and exponential
  backoff, capped `Retry-After`, per-Connector rate and concurrency limits, global concurrency,
  bounded pagination, identity-only response encoding, response byte/media checks, and
  content-addressed cache replay. Circuit state survives separate calls to one Connector instance;
  after cooldown, only one half-open probe is admitted before the circuit resets or reopens.
- Each attempt records `network_performed` as `true`, `false`, or `null`. `null` means the executor
  cannot prove whether an unexpected live Connector failure caused a network side effect. Unknown
  attempts are counted in `unknown_network_attempt_count` and are never included in the confirmed
  `live_network_attempt_count`.
- Before concurrent execution, the batch executor deterministically partitions its total response
  byte budget across active queries. A zero-share query is skipped visibly, each Connector receives
  only its assigned share, and a batch budget above the M04 download stop-policy limit is rejected.
- Successful pages retain the immutable raw-response artifact and SHA-256, query/source/Connector
  identity, parser version, page number, response size and media type, execution mode, cache-origin
  execution mode, and retrieval time. `SearchEvidenceSet` includes a raw-page manifest, and every
  evidence item resolves to one retained page while candidate observations retain the originating
  query, rank, raw-response hash, and evidence IDs. Parser version also participates in cache
  identity so stale parser output is not replayed under a different version.
  The batch executor resolves every successful page against its injected artifact store before
  accepting the outcome, so a syntactically valid but dangling artifact reference fails closed.
- Strict adapters normalize heterogeneous responses into `ConnectorRecord` without inventing
  scientific values. External excerpts remain explicitly untrusted and cannot alter the plan,
  endpoint, candidate identifiers, or system instructions.
- Candidate normalization is deterministic and order-independent. DOI, canonical HTTPS URL, then
  normalized title plus year form the deduplication precedence; compatible aliases merge replicas
  transitively while a weak title/year match cannot bridge disjoint DOI or URL identities. All
  source observations and explicit metadata conflicts are preserved.
- License labels, access state, file formats, source categories, primary-source flags, candidate
  coverage claims, and weighted assessment components are represented explicitly. The assessment
  is an initial deterministic metadata score, not a proof that a source contains valid scientific
  measurements.
- One Connector failure is isolated from other query runs. Terminal states distinguish succeeded,
  partial, needs-review, unsupported, and failed batches, and identical concurrent requests share
  one process-local execution and one immutable `connector.batch.completed` result.

## Offline acceptance fixture

The Ia supernova fixture executes all four planned source families without live network access. Its
artifact-derived acceptance metrics are eight query runs, nine pages, eight raw hits, five
deduplicated candidates, three duplicate hits, eight evidence records, zero failed queries, and
zero confirmed-live or unknown-network attempts. The extra page exercises OpenAlex pagination;
cross-source identifiers exercise provenance-preserving deduplication and conflict retention.

These numbers establish deterministic contract behavior for the fixture only. They are not a claim
that VizieR, OpenAlex, Zenodo, or Crossref returned these records in a real run.

## Verification

- `uv run pytest tests/test_connector_contracts.py tests/test_connector_registry.py
  tests/test_connector_normalizer.py tests/test_connector_http.py tests/test_connector_execution.py
  -q --no-cov` covers registry integrity, exact endpoints and credential references, all four
  adapters under Mock transport, pagination, retries, parser-version-bound cache replay,
  rate/concurrency controls, cross-call circuit cooldown/half-open recovery, identity-only encoding,
  response and batch byte bounds, redirects, credential preflight/reflection quarantine, raw-page
  manifests, tri-state network audit, normalization, provenance, conflicts, failure isolation, and
  single-flight replay.
- Mock HTTP tests use dummy credential values supplied only in memory. The packaged offline fixture
  drives the same four production adapters through `httpx.MockTransport` and asserts
  `live_network_attempt_count == 0` and `unknown_network_attempt_count == 0`.
- `uv run scidatafusion phase2-connect-demo --goal "Study Type Ia supernova light curves using
  multi-source data integration into CSV." --confirmed-by "demo-reviewer"` runs the complete
  M00-M05 offline slice and prints only safe hashes, states, and aggregate counts.
- The repository phase gate runs Ruff, format checking, strict mypy, branch-aware pytest, Bandit,
  the secret scan, and dependency audit before this checkpoint is pushed.

## Metric interpretation

M05 metrics are derived from immutable query runs, the raw-page manifest, candidate/evidence sets,
attempts, and cache outcomes. A confirmed-live count includes only explicit `true` observations;
unknown attempts remain a separate safety signal and cannot be interpreted as either live or
offline proof. Fixture metrics measure deterministic behavior and execution accounting; they do
not establish the specification's Connector success-rate, latency, duplicate-removal, source-trust
classification, or Source Recall targets. Those claims require a versioned benchmark corpus, real
credentialed source runs, sample counts, baselines, and confidence intervals.

## Known boundary

No real API credential or live external-source request is used as M05 acceptance evidence. The
default execution policy disallows network access, raw artifacts and result replay remain
process-local in-memory implementations, and runtime health is injected rather than established by
a production health service. Credential-reflection detection covers selected common encodings and
must not be treated as a complete data-loss-prevention system. M05 emits candidates and initial
coverage claims but does not choose a download set, prove Required-field coverage, or decide whether
search should continue. Those are M06 responsibilities.
