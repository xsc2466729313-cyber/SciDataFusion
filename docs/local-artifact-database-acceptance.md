# Local artifact database acceptance

Version: 1.0.0

- [x] Raw online artifacts remain immutable and content-addressed on local disk.
- [x] DuckDB stores artifact metadata, acquisition events, and failure events.
- [x] Parameterized, idempotent inserts never overwrite conflicting raw scientific content.
- [x] Source query strings are omitted while exact locator hashes preserve traceability.
- [x] The API reports the database path, counts, and stored byte total.
- [x] The delivery page shows locally persisted source materials before formal Gold publication.
- [x] Mock tests verify both local byte storage and DuckDB catalog persistence.
