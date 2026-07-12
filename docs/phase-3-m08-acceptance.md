# M08 acceptance: artifact classification and parse routing

## Exit criteria

- Strict Pydantic contracts with `extra="forbid"` cover parser capabilities, runtime and policy
  snapshots, classifications, artifact/page scopes, routes, gaps, metrics, aggregate plans,
  results, and the `parse.plan.created` event.
- M08 verifies the exact M07 request, result, completion event, artifact set, manifest, acquisition
  lineage, Bronze metadata, and bytes before planning. Every unique M07 object has exactly one
  entry and an explicit parsing or non-parsing disposition.
- Signature-first classification uses bounded, inert structural probes before untrusted MIME,
  filename, URL, or source hints. Conflicts, encryption, damage, insufficient samples, and unknown
  formats remain explicit review conditions. Truncated OOXML promotion requires a bounded EOCD,
  complete central directory, and referenced contiguous local-header cross-check.
- A content-addressed parser capability registry and separate runtime snapshot drive all routes.
  Missing, unavailable, incompatible, over-budget, or policy-blocked capabilities fail closed with
  structured gaps.
- Route selection is low-cost-first. Every executable scope names a registered primary parser,
  declared quality checks, a cost cap, and ordered conditional fallbacks. OCR/VLM/model-dependent
  parsers cannot be selected as a primary route and are never executed by M08.
- Page ranges are one-based, ordered, in-bounds, non-overlapping, and contiguous when page-level
  routing is justified by deterministic page facts. Uncertain page boundaries are not fabricated.
- Input, registry, runtime, policy, classification, route, plan, event, idempotency, and output
  hashes are recalculated. Identical requests replay one immutable checkpoint result, and
  concurrent callers share one shielded execution.
- Classifier/router output is treated as untrusted. Malformed adapter output, post-verification
  Bronze drift, checkpoint tampering, and cross-producer replay fail closed.
- M08 performs no network or model call, invokes no downstream parser, writes no Bronze bytes, and
  extracts or mutates no scientific value.
- Metrics, warnings, status, gaps, and the single privacy-reduced event are derived from the plan;
  they contain no Bronze bytes, document content, filenames, URLs, approval data, or scientific
  values.
- Contract, registry, classifier, routing, service, checkpoint, tampering, cancellation, CLI, and
  package-registry tests pass together with Ruff, format checking, strict mypy, Bandit, secret
  scanning, and dependency auditing.

## Offline Ia acceptance

`phase3-parse-plan-demo` executes the packaged M00-M08 Ia fixture. It carries the exact M07 request,
result, completion event, and in-memory Bronze objects into the production M08 planner while all
external execution remains disabled.

The fixture produces:

- status `succeeded`, execution mode `offline`, and exactly one `parse.plan.created` event;
- five Bronze objects, five classifications, five plan entries, and five routes;
- detected families: one each of PDF, HTML, plain text, CSV, and archive;
- four executable routes and one archive `metadata_only` disposition;
- three M09 document routes and one M10 table route;
- low-cost primaries `m09.pdf_text`, `m09.html`, `m09.text`, and `m10.csv`;
- one conditional PDF OCR fallback and zero high-resource primary routes;
- zero review, unsupported, failed, format-gap, capability-gap, and page-override results;
- planned cost `5000` micro-USD;
- zero external-network calls, model classifications, downstream-parser executions, and Bronze
  writes.

The accepted registry hash is
`c730fdad1494054042602cd3c09b702744ae71c82aed37e00af91618edfd1202`. Task, run, plan, snapshot,
and output hashes are emitted for each invocation and independently verified by the integrity
boundary. A new CLI invocation creates a new task/run; deterministic replay applies to an identical
validated request and is covered by service and checkpoint tests.

## Verification

- `uv run pytest tests/test_parsing_contracts.py tests/test_parsing_contract_edges.py
  tests/test_parser_registry.py tests/test_artifact_classifier.py tests/test_parse_router.py
  tests/test_parse_planning_service.py tests/test_cli.py tests/test_packaged_registries.py -q
  --no-cov` passes `134` focused tests.
- `uv run scidatafusion phase3-parse-plan-demo --goal "Study Type Ia supernova light curves"
  --confirmed-by "m08-acceptance"` reproduces the counts above without network or model access.
- The full suite contains `437` passing tests with branch-aware coverage of `90.70%`. Ruff, format,
  strict mypy, Bandit, secret scanning, and dependency auditing pass as repository gates.

## Metric interpretation

These metrics prove deterministic classification and plan construction, not downstream parse
success or scientific correctness. A planned PDF route does not prove text fidelity, reading
order, table recovery, chart extraction, field coverage, or that its fallback would pass M09's
quality checks. The Ia fixture is not a representative routing benchmark.

## Known boundary

M08 creates plans only. M09 executes document parsers and emits document/page/block IR, M10
recovers table structure, M11 handles chart digitization, and M12 parses supported scientific
formats. M08 emits none of those outputs and creates no `EvidenceAtom`, Silver value, or Gold field.

The default PDF probe is deliberately conservative. Trustworthy mixed-page decisions require
versioned page facts; documents without them remain artifact-level routes. No benchmark-backed
claim exists yet for file-type accuracy, page-route accuracy, unnecessary or missed escalation,
or P95 latency.

The checkpoint is offline. It does not discover parsers over the network, call OCR/VLM services,
authenticate to a parser, or establish live parser health. Runtime availability is an injected
snapshot. Archive containers receive container-only disposition while independently retained M07
members are routed as their own objects; M08 does not expand new archives, follow links, alter M07
review decisions, or repair missing M00 upload-byte ingestion.
