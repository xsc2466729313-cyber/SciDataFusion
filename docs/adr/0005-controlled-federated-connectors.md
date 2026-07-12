# ADR-0005: Registry-bound, controlled federated Connectors

## Status

Accepted for the M05 federated Connector checkpoint.

## Context

An M04 query identifies a source, protocol, operation, and bounded intent, but it does not authorize
an arbitrary URL or prove that a source is healthy. External APIs differ in authentication,
pagination, response shape, retry semantics, and rate limits. Allowing adapters or returned metadata
to choose network destinations would weaken SSRF controls, while embedding source-specific branches
in batch orchestration would make every new source a core-code change.

Search responses are also untrusted and nondeterministic. Candidate deduplication and assessment
must preserve raw evidence and conflicts rather than overwrite metadata or treat a relevance score
as scientific truth. Tests need to exercise the complete transport boundary without requiring real
credentials or external service availability.

## Decision

1. M05 uses a strict, size-limited, canonical-hash-verified Connector registry that must remain
   compatible with the M04 capability registry. Each descriptor pins an exact HTTPS endpoint,
   public host allowlist, read-only method, operation, dialect, parser, media types, request rate,
   and concurrency limit.
2. Credentials are dependency-injected secret values addressed by registry environment references.
   Only `OPENALEX_API_KEY` and `ZENODO_ACCESS_TOKEN` names are stored in the registry. Resolved
   values are placed only in the outbound authentication field and are not proactively persisted in
   contracts, request hashes, cache keys, application logs, artifacts, events, or the repository.
   Responses are quarantined before hashing/storage when direct or common encoded credential
   reflection is detected; this bounded check is not an exhaustive secrecy guarantee.
3. The generic executor depends on the `Connector` protocol and a source-to-implementation mapping.
   Source-specific request translation and parsing live in adapters, so adding a conforming
   Connector does not add a domain or source condition to orchestration.
4. Execution modes are explicit: `live_network`, `mock_transport`, `offline_fixture`, and
   `cache_replay`. Network access defaults to disabled, a simulated M04 plan cannot be upgraded to
   live execution, and each attempt records `network_performed` as true, false, or unknown. Unknown
   is used when an unexpected live Connector exception prevents proof of the side effect; its metric
   is separate and excluded from confirmed-live counts.
5. HTTP execution uses fixed endpoint builders, no redirect following, full timeout controls,
   bounded attempts/backoff and `Retry-After`, per-source rate/concurrency limits, global
   concurrency, identity-only response encoding, page/result/byte bounds, allowed media types, and
   content-addressed cache replay. Circuit failures persist across calls to the same Connector;
   cooldown transitions to a single half-open probe, which either resets or reopens the circuit.
6. Every successful response body is stored as an immutable content-addressed artifact before its
   normalized records become evidence. `SearchEvidenceSet` retains a raw-page manifest containing
   artifact identity, parser version, response metadata, current execution mode, and original
   execution mode for cache replay. Query, rank, raw-response hash, record hash, parser version, and
   retrieval time remain linked through evidence and candidate observations; parser version is also
   part of cache identity.
   The executor and its Connectors share an injected artifact store; every successful page
   reference must resolve through that store before the outcome is accepted.
7. Normalization and assessment are deterministic. Deduplication uses DOI, then canonical HTTPS
   URL, then normalized title/year. Compatible alias components merge transitively, but a weak
   title/year match cannot bridge disjoint strong identifiers. All observations are retained and
   conflicting metadata is emitted explicitly. Coverage and source scores state their basis and
   evidence and are not promoted to verified scientific facts.
8. The batch total-response budget is validated against the M04 download stop policy and
   deterministically divided across active queries before concurrent execution. Connectors cannot
   spend another query's assigned share, and a zero-share query is explicitly skipped.
9. A failed Connector becomes a structured query-run failure without cancelling unrelated sources.
   Batch status and metrics are artifact-derived, and process-local single-flight replay emits one
   immutable `connector.batch.completed` result for an identical request.
10. Acceptance uses `httpx.MockTransport` and offline fixture Connectors. Live source behavior and
   credentialed execution remain separate deployment and benchmark evidence.

## Consequences

- A reviewed descriptor plus a conforming Connector implementation extends discovery without
  changing the batch executor.
- Exact endpoints and disabled redirects deliberately exclude arbitrary crawl/follow behavior.
  Returned landing and license URLs are data for later controlled stages, not M05 network targets.
- Authentication, retry, rate, cache, and response policies are visible and testable, but deployment
  must still provide credential storage, durable artifacts/cache, distributed rate limiting,
  cross-worker idempotency, a real health service, and defense-in-depth secret-leak monitoring
  beyond the bounded reflection detector.
- M05 can provide normalized, provenance-rich candidates and initial assessments to M06 without
  claiming that any candidate has been selected, downloaded, parsed, or proven to cover Required
  fields.
- The offline Ia fixture proves deterministic orchestration at eight queries, nine pages, eight raw
  hits, five candidates, and zero confirmed-live or unknown-network attempts; it does not measure
  real source recall, availability, or latency.
