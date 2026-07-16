# Agent reflection and download acceptance

Version: 1.0.0

- [x] Every current-topic artifact row exposes a working attachment download action.
- [x] The endpoint accepts only a lowercase SHA-256 present in the current live-discovery snapshot.
- [x] Bronze replay rechecks the content hash before returning bytes.
- [x] HTML landing pages may yield only bounded, same-host HTTPS machine-readable attachments.
- [x] Unknown hashes are rejected without revealing arbitrary local files.
- [x] Reflection evaluates unique artifacts, machine-readable data, source diversity, and failures.
- [x] PDF and HTML files remain downloadable but do not satisfy the machine-readable data target.
- [x] Bounded JSON/text/XLSX/archive previews receive strict semantic review before target counting.
- [x] Web manifests, advertisements, and method-only tables are explicitly rejected.
- [x] Strict critic output can change only the next retrieval query, never scientific values.
- [x] Invalid critic output uses a deterministic fallback query.
- [x] Every round is idempotently checkpointed in DuckDB with a canonical proof hash.
- [x] Tests cover successful download, unknown-hash rejection, reflection continuation, target
  satisfaction, and persisted reflection events.
