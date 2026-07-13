# M11 acceptance: manually calibrated deterministic chart digitization

## Status

Accepted on 2026-07-13 for the first content-addressed offline scatter-chart slice. This is not
acceptance of OCR, VLM, automatic chart/axis/legend recognition, multi-series overlap separation,
document-region routing, arbitrary image formats, or benchmark-backed digitization accuracy.

## Definition of Done

- [x] Strict frozen Pydantic v2 contracts with `extra="forbid"` cover source, request, policy,
  runtime, axis inputs, CalibrationRecord, SeriesIR, points, FigureIR, quality, metrics, result, and
  completion event.
- [x] The source raster is immutable, content-addressed, byte/size verified, and replayed from the
  Bronze store before execution or checkpoint reuse.
- [x] Linear and log10 Decimal transforms support either pixel direction; an inverted magnitude
  axis is explicit and never inferred from a target-schema default.
- [x] Every point retains component pixels, bbox, centroid, data coordinates, x/y error, source
  hash, series identity, and both CalibrationRecord identities.
- [x] Integrity verification re-decodes pixels and recomputes calibrations and points, preventing
  rehashed fabricated values from passing replay.
- [x] Missing markers and malformed, oversized, or unsupported PPM input fail or enter review
  without invented points.
- [x] Complete results are immutable, checkpointable, single-flight, cancellation isolated,
  tamper evident, and emit one `figure.digitized` event.
- [x] No OCR, VLM, network, model, cost, final Gold write, or scientific-value mutation occurs.
- [x] Architecture, limitations, metrics, verification commands, and offline evidence are recorded.
- [x] Final repository gates and measured totals are recorded after the complete M11 run.

## Offline Ia evidence

`phase7-figure-demo` builds a stable 64-by-64 P6 PPM scatter chart, stores its exact 12,301 bytes in
the in-memory Bronze store, and supplies two confirmed anchors for each axis. Exact-color
segmentation recovers three nine-pixel marker components. Decimal calibration maps their centroids
to three time/magnitude points with 0.1 unit component-derived error. The magnitude axis is
explicitly inverted. Calibration coverage is 100%, anchor round-trip MAE is 0.0, and all points lie
inside calibrated bounds.

This is an honest proof of the deterministic calibration and segmentation core. It is not evidence
for chart-type accuracy, axis OCR accuracy, legend binding accuracy, low-confidence interception,
or normalized MAE on representative paper figures. Those claims require a versioned judged image
corpus.

The CLI exposes aggregate geometry, methods, quality, opaque identifiers, and hashes only. It omits
axis fields, units, scientific values, pixel colors, series labels, goal text, and reviewer identity.

## Verification

```powershell
uv run pytest tests/test_figure_service.py tests/test_cli.py -q --no-cov
uv run scidatafusion phase7-figure-demo `
  --goal "Study Type Ia supernova light curves using multi-source data integration into CSV." `
  --confirmed-by "m11-acceptance"
powershell -ExecutionPolicy Bypass -File scripts/check.ps1
```

The final repository gate completed with 625 passing tests and 90.04% total coverage. Ruff and
format checks passed across 214 files, mypy passed across 213 source files, Bandit reported no
issues, the secret scan passed across 696 files, and the dependency audit reported no known
vulnerabilities.

## Known boundaries

- Calibration, chart type, axis fields/units, marker color, and series name are manually supplied
  and explicitly recorded. They are not OCR/VLM predictions.
- The P6 PPM parser is deliberately narrow and bounded. PNG/JPEG/TIFF/PDF rendering needs a
  maintained image adapter and M08/M09 source-region lineage.
- Exact/tolerance color components do not solve anti-aliasing, overlapping lines, similar colors,
  bars, error bars, heatmaps, dual axes, or 3D plots.
- M13 does not yet consume FigureIR; the point flag records eligibility, not completed extraction,
  normalization, fusion, quality audit, or Gold publication.
