# M20 acceptance: quality-gated delivery, reproduction package, and workbench

## Scope

This checkpoint implements the first offline M20 vertical slice over the exact packaged M00-M19 Ia
chain. It does not resolve the three M18 review items and therefore does not claim Formal Gold.

## Definition of Done

- [x] Strict M20 request, policy, runtime, artifact, manifest, metric, result, and
  `delivery.completed` event contracts use Pydantic v2 with `extra="forbid"`.
- [x] M20 re-verifies the complete M19 and Bronze lineage before building exports.
- [x] CSV and Parquet generation requires Formal Gold and checks exact row/value equivalence.
- [x] The current failed quality gate withholds both tabular formats without creating placeholders.
- [x] A deterministic ZIP contains a canonical hash manifest, dictionary, provenance, quality,
  evidence graph, metrics, reproduction metadata, and verification notebook.
- [x] The extracted verification notebook executes and verifies every declared file hash.
- [x] Content-addressed bytes and canonical result checkpoints replay idempotently.
- [x] FastAPI exposes health, run, status, review issues, signed download tickets, and downloads.
- [x] Missing Formal Gold returns structured `409 quality_gate_failed`; invalid signatures return
  structured `403 security_policy_violation`.
- [x] The responsive workbench completes input to review-package download without reading logs.
- [x] No model, external network, scientific-value mutation, real key, or silent overwrite occurs.
- [x] ADR 0019 records the delivery gate, package, and signing decisions.

## Demonstrable path

```powershell
uv run scidatafusion phase8-delivery-demo `
  --goal "Study Type Ia supernova light curves using multi-source data integration into CSV." `
  --query "quality evidence observation time magnitude" `
  --confirmed-by "demo-reviewer"

uv run uvicorn scidatafusion.api:app --host 127.0.0.1 --port 8000
```

The CLI reports `needs_review`, three quality issues, no Formal Gold records, seven package content
artifacts, one externally referenced ZIP, zero value mutations, and zero network/model attempts.
The web page at `http://127.0.0.1:8000` runs the same deterministic chain and obtains short-lived
download tickets without exposing the in-memory signing key.

## Verification evidence

Targeted M20 tests cover review-gated delivery, package entry hashes, notebook execution, strict
contracts, tamper rejection, checkpoint replay, Formal Gold CSV/Parquet equivalence, the complete
API/UI response path, signed downloads, and invalid requests. Final repository gate totals are
639 passing tests at 90.04% branch coverage, Ruff over 228 files, strict mypy over 227 source files,
Bandit with no identified issues, a 727-file secret scan, and an audit with no known dependency
vulnerabilities. The browser acceptance used 1280x720 and 390x844 viewports; both reported zero
horizontal overflow, zero incoherent overlap, and no console errors.

## Known limits

- The packaged Ia slice has no Formal Gold because M18 has three unresolved Critical issues.
- The successful tabular serializer is tested with an exact Formal Gold contract artifact, but the
  shipped demo intentionally exercises the `needs_review` path until review resolution exists.
- Process-local stores, ephemeral ticket signing, and the offline fixture are demonstration
  boundaries, not production persistence, identity management, or live-source availability.
- HTML quality reports, durable object storage, license-specific package filtering, key rotation,
  and domain benchmark targets remain future slices.
