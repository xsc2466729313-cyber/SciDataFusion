# ADR 0013: no-guess, evidence-preserving normalization

## Status

Accepted and implemented for the first M15 offline slice.

## Context

The confirmed Ia field contracts specify target units such as `MJD` and `mag`, but the M13 table
cells do not contain independently evidenced source units or a time scale. Treating the target as
the source would silently convert an assumption into scientific data. It could also make a bare
time value appear to be MJD without proving whether it was MJD, JD, or another representation.

M15 must still turn lexical numeric values into typed, traceable values and demonstrate the
transformation-record contract without inventing context.

## Decision

1. M15 re-verifies the complete M13-M14 lineage and immutable Bronze evidence before execution or
   checkpoint replay.
2. Numeric lexical values are parsed with Python `decimal.Decimal`, must be finite, and are emitted
   in fixed-point form without float conversion. The exact input, output, hashes, decimal places,
   significant digits, formula, library version, and evidence references form one content-addressed
   `TransformationRecord`.
3. String identity values are preserved exactly. Identity preservation does not create a synthetic
   transformation record.
4. A field with a unit dimension cannot enter M16 unless its source unit is evidenced. An
   astronomical-time field also requires an evidenced time scale. Missing context produces
   explicit blocking issues while retaining the raw and parsed value.
5. `target_unit` is descriptive output intent only. It is never copied into `source_unit`, and no
   unit or time conversion is performed in this slice.
6. LLM execution, network access, unit guessing, time-scale guessing, Gold writes, and scientific
   value mutation are structurally disabled by policy and runtime contracts.

## Consequences

- Every non-identity change is reversible or exactly traceable and has a transformation record.
- The Ia fixture honestly remains partial: identifier and band are eligible for M16, while time
  and magnitude remain reviewable until source context is evidenced.
- The normalized value is serialized as an exact decimal string rather than a binary float, so no
  false precision is introduced.
- Future unit/time/coordinate adapters need an upstream evidence contract for source annotations
  and must use versioned deterministic scientific libraries. They cannot infer source context from
  the target schema.
- Process-local checkpoints prove canonical replay and conflict rejection, not durable distributed
  persistence.
