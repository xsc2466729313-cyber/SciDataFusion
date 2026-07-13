# ADR 0022: controlled online discovery and Qwen source assessment

## Status

Accepted for M21.

## Context

The connected product must support both deterministic offline replay and real web discovery. The
existing UI only executed the packaged Ia fixture even though the repository already had a Bailian
structured-output boundary. SerpApi was specified by the source documentation but not implemented.
Unrestricted browsing or allowing an LLM to edit scientific values would violate the trust model.

## Decision

1. Keep offline replay as the default. Online mode is explicit and requires
   `SCIDATA_OFFLINE_MODE=false`, a local `SERPAPI_API_KEY`, and a local `DASHSCOPE_API_KEY`.
2. Allow live search only through `https://serpapi.com/search`. Bound result count, timeout,
   retries, concurrency, request interval, and cache lifetime. Retain hashes, attempt count,
   latency, cache state, and result count without retaining a credential.
3. Use Alibaba Cloud Bailian's official OpenAI-compatible endpoint. Beijing defaults to the shared
   `dashscope.aliyuncs.com/compatible-mode/v1`; workspace-specific `*.maas.aliyuncs.com` endpoints
   remain supported. This follows the Alibaba Cloud
   [Base URL reference](https://help.aliyun.com/zh/model-studio/base-url).
4. Treat every title, URL, domain, snippet, and model response as untrusted. The Qwen prompt is
   versioned outside Python. `SourceAssessmentBatch` uses the immutable strict-contract base with
   `extra="forbid"`; unknown or duplicate source URLs invalidate the assessment.
5. Qwen may score relevance, classify likely evidence types, and recommend inspect/download/
   deprioritize. It has no contract field for scientific values and cannot modify the downstream
   deterministic parse, normalize, fuse, quality, or delivery artifacts.
6. If model validation fails after a successful search, expose a degraded result containing the
   live search evidence and the model invocation proof, but no model assessment. Do not silently
   relabel the run as offline.

The SerpApi endpoint and `q` / `api_key` / JSON result behavior follow its official
[Google Search API reference](https://serpapi.com/search-api).

## Consequences

The product can perform real search and LLM-assisted triage while preserving reproducible offline
acceptance and the evidence-first value boundary. The current slice does not automatically fetch
every live result into Bronze, bypass source licensing, or claim that a search snippet proves data
quality. Those require later authorized acquisition and corpus evaluation.
