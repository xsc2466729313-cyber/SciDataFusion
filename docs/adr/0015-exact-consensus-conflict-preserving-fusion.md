# ADR 0015: exact-consensus conflict-preserving fusion

## Status

Accepted and implemented for the first M17 offline slice.

## Context

M17 must create an analyzable Gold candidate view without losing competing scientific values or
promoting unresolved M15 context into apparent certainty. The current Ia fixture has one M16
singleton containing four normalized fields. Two fields are M16-eligible; time and magnitude retain
upstream issues because source unit or time scale is not independently evidenced.

The broader specification permits tolerance rules, uncertainty aggregation, source priority,
version precedence, and LLM-generated conflict explanations. None can be accepted safely without
registered domain rules and a labeled multi-source benchmark.

## Decision

1. M17 re-verifies the complete M13-M16 lineage and Bronze evidence before execution or checkpoint
   replay.
2. Every normalized field from every resolved record becomes an immutable fusion candidate. Raw
   and normalized forms, hashes, upstream issue count, record identity, and EvidenceAtom references
   are retained.
3. One eligible candidate may be selected. Multiple candidates may be selected only when every
   candidate is eligible and all normalized value hashes are exactly equal.
4. Distinct candidate hashes create an unresolved conflict. The fused field then has no selected
   candidate or value, preventing silent overwrite by construction.
5. A context-blocked or unevidenced candidate is withheld from the Gold candidate view even when it
   is the only value. M17 does not reinterpret M15 issues.
6. Every Gold candidate field references its resolution decision, selected candidate, all retained
   candidates, and the union of their EvidenceAtom identities. It is a candidate view for M18, not
   final Gold publication.
7. Policy and rule identities are versioned and content-addressed. Results are checkpointable,
   causally linked through `fusion.completed`, and replayed without model or network execution.

## Consequences

- The current fixture honestly selects two fields, withholds two fields, and reports zero conflicts.
  It demonstrates the no-overwrite invariant but not multi-source conflict accuracy.
- Exact consensus is deterministic and auditable, but scientifically compatible numeric values are
  not aggregated without a registered tolerance and uncertainty policy.
- Source trust, temporal precedence, experimental-condition separation, and uncertainty weighting
  remain explicit future capabilities rather than hidden heuristics.
- M18 must audit the Gold candidate dataset and resolve or route withheld/conflicting fields before
  final publication.
- Process-local checkpoints prove canonical replay and conflict rejection, not durable distributed
  persistence.
