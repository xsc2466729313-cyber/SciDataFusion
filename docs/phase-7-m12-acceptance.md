# M12 acceptance: deterministic FITS scientific-format parsing

## Status

Accepted for the first bounded, content-addressed FITS binary-table slice. This is not acceptance
of all FITS HDUs, NetCDF, HDF5, GeoTIFF, FASTA, arbitrary instrument formats, large-file Dask
execution, or representative-format success benchmarks.

## Definition of Done

- [x] Strict frozen Pydantic v2 contracts cover the scientific artifact, subset, policy, runtime
  plugin descriptor, DatasetIR, coordinates, variables, scalars, format metadata, quality,
  metrics, result, and `dataset.parsed` event.
- [x] Astropy reads the binary scientific values through the optional `scientific` install group.
- [x] Raw storage values, physical values, units, FITS formats, header cards, source row indexes,
  TSCAL/TZERO transformations, and missingness are retained without silent overwrite.
- [x] Input bytes, HDUs, headers, selected variables/rows, and materialized cells are bounded.
- [x] DatasetIR and checkpoints are content-addressed, immutable, canonical, idempotent,
  cancellation-isolated, and reverified by reparsing Bronze bytes.
- [x] Missing dependencies, unsupported HDUs, corrupt bytes, invalid subsets, runtime drift, and
  source/DatasetIR/checkpoint tampering fail with structured errors.
- [x] No model, network, cost, Gold write, or scientific-value mutation occurs.
- [x] Architecture, limitations, metrics, CLI reproduction, and offline evidence are documented.
- [x] The complete-worktree gate passes 661 tests at 90.05% branch coverage, strict mypy, Ruff,
  formatting, Bandit, secret scanning, and dependency vulnerability audit.

## Offline Ia evidence

The synthetic FITS file is 8,640 bytes and contains a primary HDU plus one `LIGHTCURVE` binary
table. M12 selects `MJD`, `MAG`, and `MAG_ERR` across four rows, materializing twelve bounded cells.
`MAG` retains raw integer storage and its `TSCAL2=0.01` / `TZERO2=10.0` physical transform. One
non-finite `MAG_ERR` remains an explicit missing value. DatasetIR replay reproduces every value and
metadata identity with zero model or network attempts.

## Verification

```powershell
uv sync --locked --group dev --extra scientific
uv run pytest tests/test_scientific_format_service.py -q --no-cov
uv run scidatafusion phase7-scientific-demo `
  --goal "Study Type Ia supernova light curves using multi-source data integration into CSV." `
  --confirmed-by "m12-acceptance"
powershell -ExecutionPolicy Bypass -File scripts/check.ps1
```

## Known boundaries

- The accepted input is a verified M08 route projection; a live scientific-file acquisition and
  full M07-M08 fixture are not claimed.
- The first adapter supports a selected FITS binary table. Image HDUs, variable-length arrays,
  multidimensional coordinates, compressed tables, and remote range reads need separate fixtures.
- Format metadata is preserved deterministically; semantic interpretation and downstream M13
  extraction from DatasetIR remain later evidence-backed integration work.
