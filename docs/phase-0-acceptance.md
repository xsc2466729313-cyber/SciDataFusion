# Phase 0 acceptance

## Scope

Engineering baseline only. Search, parsing, model calls, and front-end behavior are out of scope.

## Definition of Done

- [x] Git repository and Python 3.11 package structure
- [x] uv-managed project metadata and lockfile workflow
- [x] secret-safe environment template
- [x] strict ID, artifact reference, and event contracts
- [x] stable error codes and problem-details serialization
- [x] structured JSON logging with recursive redaction
- [x] offline `doctor` health check
- [x] unit tests and CI quality gates
- [x] baseline ADR and official requirement trace

## Verification

Verified on Windows with CPython 3.11.9 and the committed `uv.lock`:

| Check | Result |
|---|---|
| `uv run ruff check .` | passed |
| `uv run ruff format --check .` | 15 files formatted |
| `uv run mypy` | 14 files, no issues |
| `uv run pytest` | 26 passed, 98.70% coverage |
| `uv run bandit -c pyproject.toml -r src` | zero findings |
| `uv run python scripts/scan_secrets.py` | 93 files, zero findings |
| `uv run pip-audit --skip-editable` | no known vulnerabilities |
| `uv run scidatafusion doctor` | status `ok`, offline, no credentials |

The first audit found a known vulnerability in pytest 8.4.2. The lower bound was raised to
pytest 9.0.3 and the lock resolved pytest 9.1.1 before Phase 0 was accepted.

## Next checkpoint

Phase 1 implements M00-M03 only: safe task intake, scientific problem compilation, domain/task
routing, dynamic data-contract compilation, confirmation, and replayable Qwen invocation records.
