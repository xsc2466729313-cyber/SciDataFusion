# ADR-0008: Signature-first artifact classification and parse routing

## Status

Accepted for the M08 artifact-classification and parse-routing checkpoint.

## Context

M07 produces immutable, content-addressed Bronze objects plus acquisition and parent-child
provenance. Its `ContentInspection` is sufficient to control acquisition and review state, but it
is not a downstream parser decision. M08 must turn the exact M07 snapshot into an explainable plan
for M09-M12 without parsing document content, extracting tables, or transforming scientific values.

Filename extensions, declared MIME types, and source labels are untrusted routing hints. A generic
fallback such as "try PDF, then text" can waste resources, hide unsupported formats, and execute an
unregistered parser. Conversely, choosing the most capable parser first makes routine files
unnecessarily expensive. Routing must therefore be based on verified bytes, an immutable parser
capability snapshot, explicit quality gates, and a deterministic escalation policy.

Page-level routing also needs a strict boundary. Mixed text/scanned documents may require different
downstream strategies by page, but M08 must not perform OCR, VLM analysis, document reconstruction,
table recovery, or scientific extraction merely to create the route.

## Decision

1. M08 is a planning module. It emits `ArtifactClassification`, `ParserRoute`, and one aggregate
   `ParsePlan`; it does not invoke a downstream parser or emit `DocumentIR`, `TableIR`, chart data,
   scientific-file records, `EvidenceAtom`, or transformed scientific values.
2. The accepted upstream input is one integrity-valid M07 `BronzeArtifactSet` and its exact
   `ArtifactManifest`. Every planned object must resolve to an immutable M07 object ID, byte hash,
   metadata hash, and acquisition lineage. M08 reads Bronze through a hash-verifying, read-only
   boundary and never writes, replaces, annotates, or deletes Bronze objects or M07 metadata.
3. Classification is signature-first. Deterministic magic-byte, container, and bounded structural
   probes take precedence over declared MIME, content-disposition filename, URL suffix, and source
   metadata. Those secondary hints may explain a conflict but cannot override verified byte facts.
4. M08 may run bounded, deterministic classification probes that inspect headers, container
   structure, page count, text-layer density, scan indicators, and coarse table/image features.
   Such probes may produce routing facts only. They may not retain parsed body text, reconstruct
   reading order, recognize scientific values, or execute active content.
5. Parser selection is registry-driven. A content-addressed `ParserCapabilityRegistry` declares
   parser identity and version, accepted signatures and artifact kinds, output IR family, routing
   granularity, prerequisites, cost tier, deterministic quality gates, fallback compatibility, and
   whether the capability is available in the injected runtime snapshot. Adding a parser changes
   registry data and an adapter in its owning downstream module, not M08 domain condition chains.
6. Registry declarations and runtime availability remain separate. M08 binds the exact registry
   and runtime hashes into its input and output, uses only registered compatible capabilities, and
   does not perform network health checks or infer availability from a parser package name.
7. Routing is low-cost-first. The primary route is the lowest-cost healthy capability that meets
   the verified classification and mandatory output requirements. More expensive OCR, VLM,
   ensemble, or specialist routes appear only as ordered fallbacks guarded by explicit downstream
   quality-check failures and a finite maximum cost. M08 plans escalation but never executes it.
8. Page routes are optional overrides of an artifact-level route. They may be created only when a
   bounded deterministic probe establishes a valid page count and page facts. Page ranges must be
   one-based, in bounds, non-overlapping, and either cover the intended pages or leave explicit
   page gaps. Uncertain page boundaries cannot be invented and remain artifact-level review gaps.
9. Unknown, corrupt, encrypted, or unsupported formats are never coerced into PDF, text, or table
   routes. Every M07 object receives an explicit disposition: routed, container-only, needs review,
   unsupported, or deferred to a named downstream capability family. Missing capabilities produce
   structured format or capability gaps while preserving the object for later review.
10. M08 is offline and credential-free. It performs no external network or model call, does not
    follow artifact links, and cannot mutate task contracts or scientific coverage. A future model
    classifier may propose an untrusted route candidate only under a separate accepted capability;
    it cannot bypass signature checks, registry compatibility, cost limits, or strict validation.
11. The result is content-addressed and idempotent. The input hash binds the M07 artifact and
    manifest hashes, parser registry and runtime hashes, routing policy, contract version, and
    producer version. Identical inputs replay one immutable plan and emit exactly one deterministic
    `parse.plan.created` event after the complete plan passes integrity validation.
12. `parse.plan.created` carries only plan references, hashes, status, counts, gaps, and the
    idempotency key. It contains no raw Bronze bytes, parsed text, scientific values, sensitive
    filenames, URLs, authorization data, or downstream parser output.

## Consequences

- M09-M12 receive a reproducible parser plan with primary, fallback, quality-check, cost, object,
  and optional page-scope references, while retaining responsibility for parser execution and IR
  validation.
- A registry change creates a new plan identity instead of silently changing an existing route.
  Parser additions do not require edits to the generic classifier or router.
- Low-cost-first routing is bounded and explainable, but it is not a claim that the chosen route is
  globally optimal. Actual route quality can be measured only after downstream parser execution
  against a versioned benchmark.
- Signature conflicts and unknown formats remain visible. This may increase `needs_review` or
  `unsupported` results, but avoids silently treating an arbitrary byte stream as a supported
  document or scientific file.
- Page-level routing is conservative. Without trustworthy page facts, M08 emits an artifact-level
  route or a review gap rather than using OCR or VLM during planning.
- M08 does not re-evaluate Required-field coverage, validate scientific values, or establish a
  Silver/Gold evidence chain. Those responsibilities begin with downstream parsing and evidence
  modules.
