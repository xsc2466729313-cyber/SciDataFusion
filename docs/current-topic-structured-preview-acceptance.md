# M27 acceptance: current-topic structured preview

## Definition of Done

- [x] Acquired bytes are verified against the immutable Bronze SHA256 before parsing.
- [x] CSV, TSV, JSON record arrays, common record wrappers, and GeoJSON feature properties have bounded deterministic adapters.
- [x] Raw CSV strings, JSON number lexemes, booleans, strings, and nulls remain distinguishable.
- [x] Duplicate headers, ragged rows, nested cells, invalid encodings, non-standard numbers, oversized structures, and unsupported media fail closed with strict contracts.
- [x] Every displayed cell retains artifact hash, field name, row, column, parser identity, and deterministic evidence identity.
- [x] The Chinese React workbench shows real source rows and columns, field completeness, preview bounds, source link, raw download, and graph relationships.
- [x] Online previews remain non-Gold and cannot trigger semantic mapping, repair, unit conversion, conflict resolution, or quality-gate bypass.
- [x] Ruff, mypy, pytest with coverage, Bandit, secret scan, dependency audit, frontend build, Compose smoke, browser checks, and Windows clean-directory smoke pass for the final commit.
