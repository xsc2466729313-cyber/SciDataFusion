# SciDataFusion

SciDataFusion is an evidence-first AI data scientist for the 2026 Challenge Cup Alibaba
Cloud topic, Track 2 / Direction 1A: scientific data discovery, parsing, and integration.
It turns a natural-language research goal into an analysis-ready, traceable, reproducible,
and correctable scientific dataset.

The source package is being built phase by phase from the V4 specification in
[`需求分析/AI_Data_Scientist_Codex_Documentation_V4`](需求分析/AI_Data_Scientist_Codex_Documentation_V4/README.md).

## Current checkpoint

Phase 1 now turns an accepted research goal into a reviewable and confirmable scientific data
contract:

- M00 security, privacy, upload, and finite-budget intake gates;
- M01 evidence-grounded problem compilation with deterministic fallback and a validated Qwen
  provider boundary;
- M02 replayable domain/task routing with fail-closed runtime capabilities;
- M03 content-addressed Schema Packs, conflict-preserving field composition, JSON Schema, and
  immutable contract confirmation;
- an idempotent M00-M03 workflow with ordered causal checkpoints and a no-network Ia supernova
  demonstration command.

Federated source discovery starts in Phase 2; parsing, extraction, integration, and the web
workbench follow their accepted contracts in later phases.

## Quick start

```powershell
uv sync --python 3.11 --group dev
Copy-Item .env.example .env
uv run scidatafusion doctor
uv run scidatafusion phase1-demo --goal "Integrate multi-source Type Ia supernova light curves into CSV." --confirmed-by "demo-reviewer"
uv run powershell -ExecutionPolicy Bypass -File scripts/check.ps1
```

`doctor` never prints secret values. The default configuration is offline and does not call
Alibaba Cloud or any external data source. `phase1-demo` also stays offline and labels its
capability snapshot as `simulated_demo`; it is an engineering demonstration, not a production
Connector health claim.

## Quality commands

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
uv run bandit -c pyproject.toml -r src
uv run python scripts/scan_secrets.py
```

The official competition requires the final application to use Qwen through Alibaba Cloud Model
Studio or an approved competition tool. The regional provider boundary, timeout/retry/cache
controls, strict output validation, and audit record are implemented and Mock-tested. No real
credential was used in repository tests; a credentialed competition-environment proof remains a
separate deployment acceptance item.
