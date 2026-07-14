# M22 acceptance: configurable multi-query online research

## Status

Accepted for the configurable online-research boundary. Repository acceptance uses Mock transports
and secret-free configuration snapshots.

## Definition of Done

- [x] Operators can select Google or Google Scholar, search language, optional country, query-plan
  enablement, one to four queries, and one to ten retained results through environment settings.
- [x] Qwen emits a strict, versioned `SearchQueryPlan`; the user seed query is retained and duplicate
  model queries are removed before execution.
- [x] Every planned query produces an ordered success/failure record with result count and
  invocation proof when successful.
- [x] Partial search failure preserves other validated results and is visible in warnings and the
  Chinese workbench.
- [x] URLs are deduplicated and bounded before Qwen source assessment.
- [x] Planning and assessment calls have distinct model roles, prompts, schemas, and invocation
  records.
- [x] `/api/v1/online/configuration` reports effective non-secret configuration and credential
  readiness without exposing API keys.
- [x] The Chinese `联网配置` view presents providers, endpoint host, models, locale, strategy,
  limits, credential status, relevant environment-variable names, and a client-side edit form.
- [x] The edit form writes only an allowlisted subset to the local `.env`, preserves unrelated
  settings/comments, validates a temporary file before atomic replacement, and applies settings
  immediately without a server restart.
- [x] Configuration writes reject non-loopback clients; secret values are write-only, blank values
  retain existing secrets, and explicit clear controls remove them.
- [x] Strict contracts prevent the planning model from creating scientific values or mutating the
  deterministic evidence, normalization, fusion, quality, and delivery chain.
- [x] Mock tests cover locale parameters, configuration redaction, multi-query planning, duplicate
  removal, partial provider failure, and API integration.

## Deployment profile

The competition profile uses the official Bailian OpenAI-compatible endpoint and Qwen model IDs.
SerpApi remains the allowlisted search transport. Real keys are stored only in the ignored local
`.env`; repository tests do not make paid calls. The browser form is intended for a local operator,
not remote administration.
