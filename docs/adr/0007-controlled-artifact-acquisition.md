# ADR-0007: Per-hop authorized, content-addressed Bronze acquisition

## Status

Accepted for the M07 selected-source acquisition checkpoint.

## Context

M06 retains candidate locators, conservative license decisions, and download-budget reservations,
but it does not authorize arbitrary navigation or prove that a locator returns a complete, safe,
reusable artifact. M07 must preserve exact source bytes and provenance without treating response
headers, filenames, redirects, archives, or external documents as trusted input.

The downloader must also be replayable. Process-local caching alone cannot provide the same result
after a restart, while reusing an idempotency key for different producer output would make event
deduplication unsafe. Download attempts, Bronze objects, acquisitions, and completion events must
therefore be bound to one immutable request and producer version.

## Decision

1. M07 accepts one integrity-valid M06 `SelectedSourceSet`, a bounded `DownloadPolicy`, an exact
   runtime snapshot, and explicit locator-bound approvals. Non-open sources require approval for
   the initial URL and every redirect or discovered attachment before that URL is requested.
2. Requests use exact HTTPS host allowlists, manual redirect handling, disabled environment
   proxies, and no ambient credentials. Live mode additionally resolves every address as public,
   pins the connection to one validated IP, preserves the original Host header and TLS SNI, and
   requires a trusted deployment authorizer plus durable Bronze and checkpoint stores.
3. The transport enforces per-file and total byte limits, bounded chunks, connect/read timeouts,
   identity content encoding, per-host rate limits, bounded retries, exponential backoff, and a
   capped server `Retry-After` hint that cannot shorten the local backoff. Unsolicited HTTP 206,
   empty, incomplete, encoded, and oversized responses fail before persistence.
4. Query strings may be used for an authorized request but are removed from manifests. A canonical
   locator hash binds the exact URL, including its query, without persisting sensitive query text.
5. Content type is detected from bytes. Declared MIME mismatches and unknown content remain
   explicit review conditions; filenames and response MIME never override the byte inspection.
6. ZIP inspection occurs before Bronze persistence and rejects traversal, duplicate paths,
   symlinks, special or encrypted entries, CRC errors, excessive entries or expansion, nested
   archives beyond policy, and unsafe compression ratios. Archive members retain parent and member
   paths as provenance edges.
7. Bronze bytes are immutable and addressed by SHA-256. Storage never overwrites an existing
   content address, verifies bytes on replay, and deduplicates byte-identical artifacts while the
   manifest retains every root, landing-attachment, and archive-member acquisition.
8. The complete result contains an artifact set, manifest, run log, derived metrics and status,
   one `artifact.stored` event per unique object, and one `artifact.download.completed` event. All
   upstream references, attempts, acquisitions, events, and output hashes are independently
   recalculated by the integrity verifier.
9. The idempotency key binds the request, task, module, contract version, and producer version.
   Identical in-process calls use single-flight execution; a cancelled follower cannot cancel the
   owner. A strict filesystem checkpoint atomically publishes a complete result and enables a new
   service instance to replay it without network access.
10. Injected transports are borrowed by request-scoped clients and closed by the owning service,
    so different inputs may execute without closing one another's shared transport.

## Consequences

- M08 receives stable Bronze bytes, object metadata, and provenance relationships without M07
  claiming that any scientific field has been parsed or validated.
- Offline acceptance proves the complete policy and storage boundary with packaged bytes and no
  external network. It does not prove real-source availability, legal permission, or production
  object-store durability.
- Checkpoints persist complete results only. Owner cancellation or process failure during a
  download does not resume partial HTTP ranges and may leave an unreferenced immutable object for
  later garbage collection.
- The current selected-source path does not ingest M00 user-upload bytes. ZIP is the only expanded
  archive format; static HTML discovery does not execute JavaScript or authenticate sessions.
