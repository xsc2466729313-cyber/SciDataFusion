# ADR 0023: configurable multi-query online research

## Status

Accepted for M22.

## Context

M21 proved one allowlisted SerpApi query followed by one strict Qwen source assessment. That path
was safe but too rigid for open-ended scientific questions: the user had to create the only search
query, provider parameters were mostly hidden, and the UI did not expose the effective online
configuration. A flexible research workflow needs complementary searches without granting the
model permission to browse arbitrary endpoints or alter scientific values.

## Decision

1. Retain SerpApi and Alibaba Cloud Bailian as the only network boundaries for the competition
   profile. Endpoint hosts remain allowlisted; flexibility comes from bounded research strategy,
   not arbitrary operator-supplied URLs.
2. Add environment settings for Google or Google Scholar, language, optional country, automatic
   query planning, maximum query count, result count, planner model, and assessment model.
3. Ask Qwen for a strict `SearchQueryPlan` before search. The user seed query is always retained;
   duplicate model queries are removed and the final plan is capped at four entries.
4. Execute planned searches through the existing timeout, retry, rate, concurrency, cache, and
   response-validation boundary. One failed query does not discard successful queries. Every query
   receives an immutable success/failure record and successful calls retain provider proof.
5. Deduplicate validated URLs before the strict Qwen source assessment. Search planning and source
   assessment use separate prompts, schemas, model roles, and invocation records.
6. Expose `/api/v1/online/configuration` and a Chinese configuration view. They show provider,
   endpoint host, models, strategy bounds, locale, and credential readiness without returning any
   secret value.
7. Allow the local browser to update the allowlisted settings through the same configuration view.
   Writes are accepted only from loopback clients, preserve unrelated `.env` lines, use a temporary
   validated file followed by an atomic replacement, and rebuild the online service immediately.
   Secret fields are write-only: blank retains the existing value and an explicit clear action
   removes it.
8. Query planning may create search strings and evidence-type expectations only. It cannot create
   citations, URLs, scientific measurements, evidence atoms, field mappings, repairs, or Gold data.
9. After deterministic quality gates run, Qwen may propose one bounded remediation decision per
   existing issue: search for more evidence, reparse an already discovered source, keep the issue
   blocked, or request human confirmation. The application validates all issue IDs and source URLs.
   A model decision is workflow guidance, never an EvidenceAtom and never sufficient to publish
   Gold data.

## Consequences

The online branch can adapt a broad research goal into complementary discovery queries and remain
useful under partial provider failure. Offline replay and manual single-query mode remain available.
Supporting another search vendor requires a separate allowlisted adapter, contract tests, and ADR;
it cannot be enabled merely by changing an endpoint string.
