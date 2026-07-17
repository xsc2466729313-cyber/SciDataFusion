# ADR 0033: Deployable AI research service platform

Status: Accepted for v1.4.0.

## Context

The local workbench could run the complete deterministic research workflow, but larger datasets and concurrent users require durable jobs, independent workers, evidence indexing, and a modern client without weakening scientific provenance rules.

## Decision

1. Keep FastAPI and the strict Pydantic contracts as the only public service boundary. A research submission contains a bounded goal, optional query, execution mode, and idempotency key.
2. Use an in-memory repository and background task for the zero-infrastructure profile. Use PostgreSQL JSONB records, Celery, and Redis for the platform profile. Both profiles return the same job contract.
3. Execute a bounded `validate -> workflow -> evidence index` graph. LangGraph is used when installed; the deterministic direct runner preserves the same node order when optional AI packages are absent.
4. Build vector documents only from existing EvidenceAtoms. Chroma receives deterministic vectors and provenance metadata; LangChain and LlamaIndex are optional read-only views over those documents. PyTorch may validate finite vectors but cannot generate scientific values.
5. Deliver the Chinese React/TypeScript/Vite workbench through Nginx in Compose and embed the same production build in the Windows PyInstaller package. The legacy packaged page remains only a source-development fallback.
6. Keep PyTorch optional because its binary footprint is unsuitable for the default image. Operators may enable the `ai-full` dependency profile explicitly.
7. Mount one local configuration file into API and worker containers. Keys remain write-only, are never returned by the API, and are excluded from source and release artifacts.

## Consequences

- Local development still starts without PostgreSQL, Redis, Chroma, Celery, or paid APIs.
- Platform deployments gain durable idempotent job records, asynchronous workers, and evidence retrieval without changing scientific result contracts.
- Optional framework failures degrade capability reporting or indexing; they do not bypass validation, evidence binding, or the Gold quality gate.
- The 3D graph is a visualization of immutable relationships. Dragging nodes or changing the camera never mutates evidence.
- The default Docker build is smaller, while a full PyTorch image remains reproducible through one explicit build argument.
