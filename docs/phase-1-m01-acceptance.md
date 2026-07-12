# M01 acceptance: scientific problem compiler

## Exit criteria

- Only an accepted M00 `TaskEnvelope` can be compiled.
- The original research goal is immutable and every inferred entity, variable, condition, scope,
  and problem unit carries an exact source span and confidence.
- Unknown information stays unknown; missing entity/variable and qualitative scope produce one
  minimal clarification request instead of invented scientific values.
- External/Qwen candidates cross the same strict `CandidateBatch` and grounding validator.
- Invalid model JSON or invented spans fall back to deterministic extraction and remain visible
  in warnings.
- Bailian records retain provider, regional endpoint host, requested/actual Qwen model, token use,
  latency, attempts, cache state, and request/response hashes without prompt or credential values.

## Verification

- M01 deterministic compiler: 17 tests passed.
- Qwen adapter and fallback audit: 2 tests passed.
- Bailian timeout/retry/cache/response boundary: 4 tests passed.
- Ruff and mypy passed for 11 M01/model source and test files.
- Network behavior is tested with `httpx.MockTransport`; no real credential or model call is used.

## Known boundary

The deterministic fallback intentionally recognizes a limited cross-domain vocabulary. Broader
coverage comes from validated Qwen candidates and later versioned terminology/domain packs; it is
not presented as the final benchmark corpus.
