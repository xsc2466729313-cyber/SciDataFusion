# ADR 0030: Automatic live material acquisition

Status: Accepted for v1.3.0.

## Context

Online research stopped after candidate-source discovery. The UI correctly showed source URLs, but
no current-topic bytes entered the immutable Bronze layer, leaving every downstream parser without
input and making the workflow appear stuck at automatic evidence collection.

## Decision

1. Automatically acquire up to five HTTPS sources whose strict Qwen assessment recommends
   `download`. If the model recommends only `inspect`, deterministically promote at most two
   candidates, prioritizing direct scientific files over landing pages and then relevance score.
   URLs must originate in validated search results; model-created URLs are rejected before this
   stage.
2. Build a request-scoped exact-host allowlist from only those selected URLs. DNS-pin every host and
   reject non-public, private, reserved, loopback, or rebinding-prone addresses.
3. Disable redirects, credentials, ambient proxy configuration, cookies, and content encoding.
   Bound each file to 10 MB, the run to 25 MB, reads to 10 seconds, and each host to one request per
   second.
4. Inspect response bytes rather than trusting filenames or media headers, then store successful
   bytes immutably by SHA-256 in the Bronze store.
5. Preserve every failed attempt as a strict code, retryability flag, exact locator hash, and source
   identity. One failure never blocks other selected sources.
6. Acquisition does not authorize scientific values. Only later deterministic parsers and
   evidence-backed mapping may populate fields or release Gold data.

## Consequences

- Live discovery now automatically produces real, content-addressed current-topic artifacts.
- The workbench exposes acquisition successes and failures instead of remaining at an unexplained
  empty-file state.
- Sources requiring authentication, redirects, larger files, or unsupported formats remain
  explicit gaps for later connector/domain-pack slices.
