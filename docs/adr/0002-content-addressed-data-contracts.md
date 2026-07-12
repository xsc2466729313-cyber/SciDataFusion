# ADR-0002: Content-addressed scientific data contracts

## Status

Accepted for M03.

## Context

Discovery, extraction, normalization, and fusion must agree on the same field definitions. A
mutable dictionary or an unchecked model-generated schema would allow silent type/unit changes,
lost user constraints, and irreproducible downstream results.

## Decision

1. Domain and task field fragments live in a strict, canonical-hash-protected Schema Pack registry.
   Each fragment also records the exact M02 pack-manifest hash it extends.
2. Deterministic code composes fields, units, entity keys, provenance, quality gates, output
   preferences, constraints, and assumptions. Model output is not authoritative at this boundary.
3. Compatible field definitions are rebuilt through Pydantic validation. Incompatible definitions
   produce a blocking issue containing both definitions.
4. Product-level variables may map to a selected task pack's complete field set. Unknown variables
   remain visibly unresolved and are never assigned a guessed scientific type.
5. The contract hash covers all scientific semantics and the Schema Pack snapshot. The contract ID
   is the first 128 bits of that hash; confirmation recomputes the full hash before state transition.
6. Upstream assumption status is retained as `source_status`. Confirming the contract means the
   reviewer accepts the frozen contract as a whole without rewriting its historical M01 record.

## Consequences

- Downstream modules can validate one immutable contract and replay the exact schema decision.
- Registry or contract tampering fails before confirmation.
- Schema conflicts and unknowns require review instead of being hidden by best-effort merging.
- Any semantic change creates a different hash/ID and is exposed by `ContractDiffService`.
- The current in-memory idempotency cache is process-local; durable compare-and-set storage remains
  required before multi-worker deployment.
