# SciDataFusion

SciDataFusion is an evidence-first AI data scientist for the 2026 Challenge Cup Alibaba
Cloud topic, Track 2 / Direction 1A: scientific data discovery, parsing, and integration.
It turns a natural-language research goal into an analysis-ready, traceable, reproducible,
and correctable scientific dataset.

The source package is being built phase by phase from the V4 specification in
[`需求分析/AI_Data_Scientist_Codex_Documentation_V4`](需求分析/AI_Data_Scientist_Codex_Documentation_V4/README.md).

## Current checkpoint

Phase 0 establishes the repository and contract baseline only:

- Python 3.11 project and reproducible dependency lock;
- strict settings and secret-safe diagnostics;
- typed IDs, content hashes, artifact references, and immutable event envelopes;
- structured JSON logging and stable application error codes;
- local quality checks and CI.

Search, document parsing, scientific value extraction, and the web application deliberately
start in later phases after their contracts are accepted.

## Quick start

```powershell
uv sync --python 3.11 --group dev
Copy-Item .env.example .env
uv run scidatafusion doctor
uv run powershell -ExecutionPolicy Bypass -File scripts/check.ps1
```

`doctor` never prints secret values. The default configuration is offline and does not call
Alibaba Cloud or any external data source.

## Quality commands

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
uv run bandit -c pyproject.toml -r src
uv run python scripts/scan_secrets.py
```

The official competition requires the final application to use Qwen through Alibaba Cloud
Model Studio or an approved competition tool. The integration is configured through
environment variables and will be implemented behind a provider boundary in Phase 1.
