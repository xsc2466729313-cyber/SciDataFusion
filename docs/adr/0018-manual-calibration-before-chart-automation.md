# ADR 0018: manual calibration before chart automation

## Status

Accepted and implemented for the first M11 offline slice.

## Context

M11 must convert chart pixels into traceable values without assuming a linear forward axis or
allowing an LLM to invent ticks, units, or series semantics. The packaged M00-M19 Ia chain has no
recoverable high-resolution figure region. Claiming automatic paper-chart digitization against
that input would be false.

The repository also does not yet contain a controlled OCR or vision adapter. A deterministic
calibration core can still be accepted independently using immutable raster bytes and explicit
human-confirmed tick anchors.

## Decision

1. The first slice accepts one direct content-addressed P6 PPM figure and a confirmed scientific
   contract. It does not claim M08/M09 figure-region integration.
2. Each axis requires exactly two distinct, manually confirmed pixel/value tick anchors, an axis
   scale, field, unit, and explicit inverted state.
3. Linear and log10 transformations use `python.decimal` at a recorded precision. CalibrationRecord
   preserves anchors, transformed anchors, slope, intercept, formula, scale, direction, and runtime
   identity.
4. A bounded fixture-grade PPM adapter parses exact bytes. Exact/tolerance RGB segmentation creates
   connected components; each retained component produces one point from its centroid.
5. Every point preserves its component bbox and size, pixel coordinates, data coordinates,
   component-derived x/y error, series identity, both calibration identities, and source-byte hash.
6. Integrity replay decodes the Bronze bytes again, re-segments components, rebuilds both Decimal
   transforms, and recomputes every point. Recomputed internal hashes alone cannot legalize a
   fabricated scientific value.
7. Missing markers or unsupported raster structure produces an explicit review/error path. OCR,
   VLM, network, and automatic semantic decisions remain disabled.

## Consequences

- The synthetic Ia fixture demonstrates three genuinely extracted raster components with complete
  calibration coverage and an explicitly inverted magnitude axis.
- Results remain partial because tick, field, unit, chart type, and series semantics are manual
  assertions requiring independent review.
- The simple PPM adapter is an acceptance fixture boundary, not a general scientific-image parser.
- Automatic axis/tick/legend recognition, complex plots, overlapping series, dual axes, source
  FigureRegion integration, and benchmark accuracy require later accepted adapters and corpora.
- Process-local checkpoints demonstrate canonical replay, not durable distributed persistence.
