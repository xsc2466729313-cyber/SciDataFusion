# M00 acceptance: task intake, security, and budget

## Exit criteria

- Only an accepted `TaskEnvelope` can enter downstream modules.
- URL preflight blocks credentials, unsafe schemes, private/link-local/metadata addresses, and
  any private address returned in a mixed DNS response.
- Upload manifests enforce media type, extension, file/total size, archive entry, expansion, and
  compression-ratio limits before extraction.
- Requested budgets above hard limits are rejected rather than silently clamped.
- Equal idempotency keys replay one immutable result; reuse with different input fails explicitly.
- Restricted tasks disable external model calls without blocking permitted local processing.

## Verification

- `uv run pytest tests/test_intake.py -q --no-cov`: 21 passed.
- Ruff passed for the M00 contract, implementation, and tests.
- mypy passed for 9 M00 source/test files.
- Tests use an injected fake DNS resolver and make no real network requests.

## Known boundary

M00 validates archive metadata supplied at the upload boundary. Byte-level archive directory
inspection begins when immutable upload storage is implemented in M07.
