# ADR 0031: DuckDB catalog for local online artifacts

Status: Accepted for v1.3.0.

## Context

Content-addressed online bytes were present under `var/online-bronze`, but the product only rendered
formal delivery-package artifacts. Users therefore saw an empty delivery table, and no queryable
database connected files to acquisition facts or failures.

## Decision

1. Keep raw files immutable in the SHA-256 Bronze directory; DuckDB stores metadata and lineage,
   never a mutable replacement for raw bytes.
2. Maintain `online_artifacts`, append-only `online_acquisition_events`, and
   `online_acquisition_failures` tables in `var/online_artifacts.duckdb`.
3. Parameterize every SQL write. Artifact and event identities are content/locator hashes, making
   retries idempotent and preventing silent overwrites.
4. Persist sanitized source URLs without query strings, plus the exact locator hash, source title,
   media type, artifact kind, local storage URI, size, status, and timestamp.
5. Return strict catalog counts and the local database path in the workbench API, and render locally
   persisted artifacts in the delivery view even before Gold is eligible.

## Consequences

- Users can immediately see and query locally downloaded materials without confusing them with
  quality-approved Gold output.
- Repeated runs deduplicate raw files and acquisition events while retaining structured failures.
- Database loss does not destroy raw evidence; the immutable Bronze files remain independently
  content-verifiable.
