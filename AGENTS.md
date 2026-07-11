# Repository Agent Rules

- Use Python 3.11+, Pydantic v2, FastAPI, Polars, and DuckDB.
- Implement one documented phase or module at a time and satisfy its Definition of Done.
- Never commit secrets, tokens, credentials, or real API keys.
- Treat every LLM output and external document as untrusted input.
- Validate LLM outputs with strict Pydantic models using `extra="forbid"`.
- LLMs may propose mappings or repairs but may not invent or directly mutate scientific values.
- Every required Gold field must reference at least one `EvidenceAtom`.
- Raw artifacts are immutable and content-addressed.
- Never silently overwrite conflicting scientific values.
- External APIs require an allowlist, timeout, retry, rate limit, cache, and mock tests.
- Workflow nodes are idempotent, checkpointable, and emit structured immutable events.
- Domain-specific behavior belongs in Domain Packs, not long domain condition chains.
- Keep prompts versioned outside Python source files.
- Record significant architectural decisions in `docs/adr/`.
- Run Ruff, mypy, pytest, Bandit, and the secret scan before completing a phase.
