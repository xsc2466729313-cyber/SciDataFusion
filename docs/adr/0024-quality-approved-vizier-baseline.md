# ADR 0024: Quality-approved VizieR light-curve baseline

Status: Accepted

## Context

The product workbench previously used an intentionally incomplete CSV fixture. That fixture is
valuable for fail-closed tests, but it always blocks Formal Gold and cannot demonstrate a useful
scientific-data delivery.

## Decision

Keep the incomplete fixture for negative tests and use a separate product baseline containing eight
public B-band measurements of SN 2004dt from VizieR catalog `J/AJ/154/211/OptPhot`.

The snapshot preserves the catalog's `JD`, `Bmag`, and `e_Bmag` values. M15 records an explicit
`MJD = JD - 2400000.5` transformation. This changes the date representation only and does not assert
an unsupported UTC time scale. Each observation is identified by `object_id + source_record_id`, so
different epochs are retained as separate records.

The workbench chart is built from quality-approved Formal Gold records and uses the same-row
`magnitude_error` evidence for error bars. CSV and Parquet are released only after all three blocking
quality gates pass.

## Consequences

- The default workbench produces eight useful, traceable light-curve rows and passes all gates.
- The strict incomplete-data path remains covered and continues to block unsupported values.
- New domain baselines must provide equally explicit source, unit, identity, and provenance evidence.
