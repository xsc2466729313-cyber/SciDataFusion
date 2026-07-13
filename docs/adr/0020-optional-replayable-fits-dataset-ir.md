# ADR 0020: optional, replayable FITS DatasetIR parsing

## Status

Accepted and implemented for the first M12 offline slice.

## Context

Scientific formats contain typed binary arrays, coordinate metadata, scale/offset rules, missing
sentinels, and format-specific headers that a generic CSV parser cannot preserve. M12 also needs to
remain extensible without importing every domain library into the generic workflow core.

## Decision

1. Scientific formats implement a typed plugin protocol. Astropy FITS is packaged in the optional
   `scientific` dependency group and imported only when the FITS plugin is constructed.
2. The first slice accepts an immutable route projection with an explicit HDU, ordered variable
   list, and half-open row range. Policies bound input bytes, HDUs, header cards, rows, variables,
   and materialized cells before values are returned.
3. DatasetIR retains format metadata, row coordinates, FITS storage types, units, null markers,
   raw storage values, decoded physical values, and explicit linear scale records. Non-finite
   values become typed missing values, never replacement numbers.
4. DatasetIR and complete results are content-addressed. Checkpoint replay rereads Bronze bytes,
   reruns the pinned plugin over the same subset, and compares the reconstructed DatasetIR.
5. The plugin performs no network or model calls. Semantic interpretation remains a candidate for
   later evidence-backed domain logic and may not mutate binary scientific values.

## Consequences

- The accepted FITS fixture proves one bounded binary-table path, not arbitrary FITS images or
  representative-format success rates.
- NetCDF, HDF5, GeoTIFF, FASTA, and instrument formats require separate registered adapters and
  fixtures. Missing optional dependencies fail with an explicit installation-group hint.
- A complete M07-M08 acquisition chain for scientific files and large-file server-side chunking
  remain future integration work; the first slice verifies an immutable M08 route projection.
