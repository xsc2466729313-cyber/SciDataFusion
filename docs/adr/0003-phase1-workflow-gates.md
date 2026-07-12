# ADR-0003: Fail-closed Phase 1 workflow gates

## Status

Accepted for the Phase 1 integration checkpoint.

## Context

M00-M03 each expose a valid partial artifact, including review and unsupported outcomes. Merely
chaining method calls could route an unresolved problem, enable packs from declared requirements
instead of live capabilities, or confirm a client-modified contract.

## Decision

1. Phase 1 advances only from accepted M00 output to an M01 result with no blocking clarification.
2. A confirmable contract additionally requires M02 `succeeded + formal`, no missing capability,
   no proposed pack, and an M03 `draft` with no warning or conflict.
3. The runtime default supplies zero capabilities. Only the named offline demo builder supplies the
   union of registry-declared requirements, and every result labels that snapshot `simulated_demo`.
4. Confirmation accepts only `contract_id`, expected hash, and reviewer context, then reads the
   server-issued draft from the workflow's internal store. It never trusts a returned client model,
   and process-local reentrant locks make the confirmation compare-and-set atomic across threads.
5. Request execution is serialized and cached by canonical request hash in this baseline, avoiding
   concurrent duplicate model/events inside one process. `force_recompute` is not public.
6. Checkpoints use unique event IDs, contiguous sequence numbers, and explicit causation IDs. The
   M02 event ID binds task, run, and semantic decision hash.
7. Cross-stage validation binds the M00 research goal to M01, the exact M01 routing projection to
   M02 `input_hash`, and the M01 problem/M02 decision IDs to M03.
8. Public CLI output excludes research text, evidence spans, URLs, artifact URIs, model bodies,
   credentials, and reviewer identity.
9. When task policy disallows an external model, the workflow selects the injected local compiler
   even if an external compiler was configured.
10. The external M01 compiler declares a 4096-token reservation matching its structured request.
   When the accepted M00 allocation is smaller, the workflow uses the local compiler and records a
   non-blocking budget fallback.

## Consequences

- Review/unsupported artifacts remain inspectable but cannot be mistaken for confirmed output.
- The Ia demo is reproducible offline without claiming production Connector health.
- Confirmation and execute-once guarantees remain process-local. Multi-worker deployment requires
  durable immutable artifacts, unique idempotency constraints, transactional outbox events, and an
  authenticated reviewer identity supplied by the API layer.
