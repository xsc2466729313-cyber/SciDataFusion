# M21 acceptance: controlled live discovery and Qwen source assessment

## Status

Accepted for the dual-mode discovery boundary. Repository acceptance uses Mock transports and no
real credential, paid request, or uncontrolled website fetch.

## Definition of Done

- [x] The Chinese workbench exposes explicit `离线复现` and `联网智能` modes and reports runtime
  readiness without returning secret values.
- [x] SerpApi is restricted to `serpapi.com` with timeout, retry, result, concurrency, rate, and
  cache bounds plus a secret-free invocation record.
- [x] Search JSON and every displayed URL/title/domain/snippet are validated as untrusted input.
- [x] Qwen uses an external versioned prompt and strict Pydantic `extra="forbid"` output.
- [x] Qwen can only assess source relevance, likely evidence type, rationale, and next action; its
  schema contains no scientific value or mutation operation.
- [x] Invalid JSON, extra fields, duplicate URLs, and unknown URLs discard model assessments.
- [x] Model failure degrades to validated search results and preserves honest provider proof.
- [x] API integration connects online results to the complete workbench snapshot while leaving the
  deterministic parsing, evidence, quality, and delivery chain unchanged.
- [x] Mock tests cover success, retry, cache, redaction, offline blocking, malformed responses,
  strict model rejection, degraded operation, runtime readiness, and FastAPI integration.
- [x] Desktop 1280x720 and mobile 390x844 browser acceptance covers all five views, dual-mode
  state, runtime proof, one-click execution, no page-level overflow, and a clean application
  console. The complete gate passes 661 tests at 90.05% branch coverage plus Ruff, formatting,
  strict mypy, Bandit, secret scan, and dependency audit.

## Local online configuration

Real keys belong only in the ignored `.env` file. Set `SCIDATA_OFFLINE_MODE=false`,
`SERPAPI_API_KEY`, and `DASHSCOPE_API_KEY`, then restart Uvicorn. The Beijing shared Bailian base URL
is resolved automatically. `/api/v1/runtime` must return `online_ready=true` before the online mode
is enabled in the UI.

## Known boundaries

- Search snippets are discovery evidence, not parsed scientific evidence or proof of data quality.
- M21 does not automatically download arbitrary result URLs. Authorized acquisition still passes
  through M07 allowlists, budgets, immutable Bronze storage, and licensing decisions.
- Live provider uptime, search quality, cost, and model relevance require a separate judged online
  evaluation and are not inferred from Mock acceptance.
