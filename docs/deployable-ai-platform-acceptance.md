# M26 acceptance: deployable AI research service platform

## Status

Accepted for the v1.4.0 source, Docker Compose, and Windows portable profiles after the repository gate and runtime smoke tests pass.

## Definition of Done

- [x] A strict research-job API supports submit, list, get, idempotent replay, bounded pagination, and secret-safe failures.
- [x] Local mode completes jobs without infrastructure; platform mode persists jobs in PostgreSQL and dispatches them through Celery and Redis.
- [x] Worker nodes reload the shared local configuration and validate the persisted submission before execution.
- [x] Chroma documents are derived only from existing evidence and retain task, source, location, trust, and content identity metadata.
- [x] LangGraph provides a bounded workflow with a deterministic fallback; optional LangChain, LlamaIndex, scikit-learn, and PyTorch capabilities are reported honestly.
- [x] The Chinese React/Vite workbench accepts a research direction, polls asynchronous jobs, and presents adaptive sources, evidence, issues, artifacts, and configuration.
- [x] The 3D knowledge graph supports rotation, zoom, dragging, click inspection, relation highlighting, stable dimensions, and responsive layout.
- [x] Docker Compose defines frontend, API, worker, PostgreSQL, Redis, and Chroma services with health checks, persistent volumes, and no embedded credentials.
- [x] The Windows build embeds the production React assets and keeps the local API bound to loopback.
- [x] Desktop and mobile browser checks cover nonblank rendering, no horizontal overflow, configuration states, job completion, and graph interaction.
- [x] Ruff, mypy, pytest with coverage, Bandit, secret scan, dependency audit, Compose startup, and clean-directory Windows executable smoke tests pass for the final commit.

## Scientific boundary

LLMs and external documents remain untrusted. AI may plan searches, assess sources, suggest mappings, or propose review actions. It cannot create EvidenceAtoms, directly mutate scientific values, resolve unsupported conflicts, or release Gold data that failed deterministic quality gates.
