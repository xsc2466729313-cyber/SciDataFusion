# ADR-0001: Phase 0 engineering baseline

- Status: Accepted
- Date: 2026-07-11

## Context

The V4 package contains specifications but no executable project. Later modules need stable
contracts, repeatable tooling, secret-safe configuration, and immutable event records before
network, model, parser, or data-store behavior is introduced.

## Decision

- Use a `src/scidatafusion` package on Python 3.11.
- Use uv for lockfile-based environments and Hatchling for packaging.
- Use Pydantic v2 for strict, frozen cross-module contracts.
- Keep Phase 0 dependencies minimal; FastAPI and analytical data dependencies remain optional
  until their implementation phases.
- Use standard-library JSON logging with recursive credential redaction.
- Store large artifacts outside workflow state and reference them by SHA-256.
- Keep the runtime offline by default. Online mode requires an explicit DashScope credential.
- Derive the Bailian endpoint from its region and Workspace ID. Online overrides are restricted
  to official Alibaba Cloud HTTPS hosts so credentials cannot be sent to an arbitrary endpoint.
- Use Ruff, mypy, pytest, Bandit, a local secret scan, and pip-audit as release checks.

## Consequences

The repository has an executable and testable baseline without pretending that search, parsing,
or model calls already exist. Later contracts must extend these primitives rather than pass raw
dictionaries across modules.
