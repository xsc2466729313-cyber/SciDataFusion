# ADR 0016: contract-driven quality gates before formal Gold

## Status

Accepted and implemented for the first M18 offline slice.

## Context

M17 produces an auditable Gold candidate view, not a final dataset. The current Ia candidate has
two selected fields and two withheld fields; the confirmed contract also requires a source record
identifier that was absent upstream. Publishing those partial candidates as formal Gold would hide
known completeness and provenance failures.

The broader M18 specification permits domain validators, statistical checks, bounded retries,
conversion rules, and LLM-generated review explanations. The current evidence does not authorize a
scientific-value conversion or a deterministic repair.

## Decision

1. M18 re-verifies the complete M13-M17 lineage and Bronze evidence before execution or checkpoint
   replay.
2. The first slice evaluates the quality gates already frozen in the confirmed scientific contract:
   required fields, any-of fields, and field provenance. Generic core code does not branch on the
   scientific domain.
3. Every failed record-level gate creates one immutable `QualityIssue` with severity, evidence
   references, affected fields, structured code, suggested whitelist action, and open status.
4. Every issue creates exactly one repair-plan step and one review-queue item. Blocking failures use
   `request_human`; no automatic repair or scientific-value mutation is enabled in this slice.
5. The repair plan records impacted M13-M17 nodes, attempt zero, and a bounded maximum. Since no
   action executes, before/after score and issue count must remain identical.
6. A `FormalGoldDataset` may exist only when at least one record exists and every blocking quality
   gate passes. Otherwise the result is reviewable and formal Gold is absent by contract.
7. All artifacts are content-addressed, checkpointable, causally linked through `quality.gated`,
   and replayed without model or network execution.

## Consequences

- The current fixture honestly reports three Critical issues and zero formal Gold records.
- Missing values and source context remain visible instead of being repaired from target-schema
  assumptions.
- Contract gates are deterministic and reusable across domains, but they do not establish anomaly-
  detection recall or repair accuracy.
- Domain validators, uncertainty/range checks, registered conversion rules, local partial retries,
  rollback, and reviewer resolution require later accepted slices.
- Process-local checkpoints prove canonical replay and conflict rejection, not durable distributed
  persistence.
