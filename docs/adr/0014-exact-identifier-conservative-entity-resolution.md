# ADR 0014: exact-identifier conservative entity resolution

## Status

Accepted and implemented for the first M16 offline slice.

## Context

M16 must distinguish entity identity from record duplication without turning similar names or
incomplete scientific context into automatic merges. The current Ia fixture has one normalized
record. Its `object_id` entity key is evidence-backed and M16-eligible, while time and magnitude
remain blocked by M15 issues.

The broader specification allows probabilistic and semantic candidate comparison, but accepting
those mechanisms without a labeled multi-record corpus would create an unmeasured false-merge risk.

## Decision

1. M16 re-verifies the complete M13-M15 lineage and Bronze evidence before execution or checkpoint
   replay.
2. Every contract entity key must be present, M16-eligible, and have a normalized value hash. The
   ordered key names and hashes produce a privacy-reduced exact entity fingerprint.
3. Candidate generation first buckets records by exact entity fingerprint. Pair metrics are
   computed within buckets; the implementation never constructs all possible `O(n^2)` pairs.
4. A one-member bucket is an evidenced singleton, not an automatic merge. A multi-member bucket is
   automatically merged only under the exact stable-identifier rule with score and threshold 1.0.
5. Record duplication is separate from entity identity. A duplicate group requires at least two
   records in one entity cluster and equality of every M16-eligible field hash.
6. Missing or blocked entity keys produce unresolved records. They cannot join a cluster through
   fuzzy similarity, an LLM decision, or a target-schema assumption.
7. All artifacts are content-addressed, checkpointable, causally linked through `entity.resolved`,
   and contain evidence hashes rather than raw entity-key values.

## Consequences

- The current fixture honestly yields one singleton cluster and zero duplicate groups. It does not
  claim to validate cross-source matching accuracy.
- Exact stable identifiers provide explainable, deterministic automatic merges with no model or
  network dependency, but aliases and identifier registries remain unsupported.
- Same-entity observations with different eligible values can share a cluster without being marked
  duplicate. M17 receives the cluster and owns conflict-preserving fusion.
- Fuzzy matching, semantic candidate explanations, transitive-closure review, stable-ID conflict
  hypotheses, and rollback benchmarks require a representative multi-record Domain Pack fixture.
- Process-local checkpoints demonstrate canonical replay and conflict rejection, not durable
  distributed persistence.
