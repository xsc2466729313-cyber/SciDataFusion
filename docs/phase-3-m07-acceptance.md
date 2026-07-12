# M07 acceptance: controlled acquisition and immutable Bronze storage

## Exit criteria

- Strict Pydantic contracts cover requests, policy and runtime snapshots, approvals, response and
  content inspection, immutable Bronze objects, acquisitions, manifests, run logs, metrics, and
  `artifact.stored` / `artifact.download.completed` events.
- M07 verifies the exact M06 selection hash and every request, policy, runtime, object, acquisition,
  attempt, event, artifact-set, manifest, run-log, idempotency, and output hash before replay.
- Non-open sources require an exact locator approval. Redirect targets and discovered attachments
  are authorized before their request, and safe manifests omit URL queries and hostile HTML links.
- HTTPS allowlists, manual redirects, public DNS validation and IP pinning, disabled environment
  proxies, timeouts, per-host rate limits, bounded retries/backoff, cache accounting, and byte
  budgets fail closed. HTTP 206, empty, incomplete, encoded, and oversized responses are rejected.
- Byte inspection determines media type. Unknown content or a declared/detected mismatch remains a
  derived review condition instead of being silently trusted or discarded.
- ZIP inspection rejects traversal, duplicate paths, links, special files, encryption, CRC errors,
  excessive entries, member or total expansion, nested archives beyond policy, and compression
  bombs before the archive is stored.
- Bronze stores use SHA-256 content addresses, no-overwrite publication, replay verification, and
  cross-source byte deduplication. The manifest retains root, landing-attachment, and archive-member
  relationships even when multiple acquisitions resolve to one object.
- Attempt sequences are contiguous and terminate with `retryable=false`. Recovered transient
  failures remain audited without forcing an otherwise complete result to remain partial.
- Identical calls use single-flight execution. Complete results can be atomically checkpointed and
  replayed across service instances without transport access; injected transport ownership is
  explicit and safe across different inputs.
- Metrics, warnings, and status are artifact-derived. Public CLI output contains hashes and counts,
  not research text, reviewer identity, URLs, filenames, approval references, or source content.

## Offline Ia acceptance

`phase3-download-demo` executes M00-M07 over the packaged Ia fixture with external network disabled.
It returns `partial` because one selected M06 source has only an unsupported non-URL locator, while
the supported sources are acquired successfully.

The fixture produces:

- execution mode `offline_fixture` and `network_performed == false`;
- three selected sources and five attempts;
- stored / deduplicated / skipped attempts: `3 / 1 / 1`;
- failed / quarantined / cache-hit attempts: `0 / 0 / 0`;
- six acquisitions: three roots, one landing attachment, and two archive members;
- five unique Bronze objects and zero review-required objects;
- detected PDF, ZIP, CSV, HTML, and plain-text content;
- `590` received HTTP bytes and `612` unique persisted bytes;
- five `artifact.stored` events and one `artifact.download.completed` event.

Persisted unique bytes exceed received bytes because the Bronze set includes validated ZIP member
bytes. The fixture's archive-member CSV and separately downloaded CSV are byte-identical, so they
produce one content-addressed object with two acquisition edges.

## Verification

- `uv run pytest tests/test_artifact_contracts.py tests/test_artifact_primitives.py
  tests/test_artifact_downloader.py tests/test_artifact_service.py -q --no-cov` covers contracts,
  authorization, streaming failures, DNS pinning, 206 rejection, archives, storage, retry terminal
  state, cancellation isolation, transport ownership, checkpoint replay, and tampering.
- `uv run pytest tests/test_cli.py -q --no-cov` verifies the privacy-reduced M00-M07 summary.
- `uv run scidatafusion phase3-download-demo --goal "Study Type Ia supernova light curves using
  multi-source data integration into CSV." --confirmed-by "demo-reviewer"` reproduces the counts
  above without external network access.
- The full suite contains `314` passing tests with branch-aware coverage of `90.02%`. Ruff, format,
  strict mypy, Bandit, secret scanning, and dependency auditing are phase gates.

## Metric interpretation

These metrics describe controlled acquisition and immutable provenance. They do not measure parsed
row completeness, scientific correctness, extraction accuracy, or final dataset quality. The
fixture is not a production availability or malware benchmark and does not establish the V4 target
download-success or malicious-download-block rates.

## Known boundary

M07 currently accepts M06 selected-source locators, not M00 user-upload bytes. It creates Bronze
artifacts only; M08-M10 must plan and execute document/table parsing before Silver coverage can be
re-evaluated, and no Gold-to-Bronze evidence chain exists yet. The filesystem store detects
tampering but is not WORM cloud storage. A storage failure may leave an immutable object that is not
referenced by the final manifest.

The checkpoint replays complete results; it is not an HTTP Range or mid-download resume mechanism,
and owner cancellation is not yet persisted as a partial checkpoint. HTML attachment discovery is
static, non-recursive, and does not execute JavaScript or authenticate. ZIP is the only expanded
archive format; gzip, tar, and nested archives are retained or rejected rather than recursively
expanded. No real external network or legal approval is claimed by this acceptance record.
