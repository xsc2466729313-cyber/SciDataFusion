# Phase 1 acceptance: research goal to confirmed data contract

## Demonstrable exit

The offline Ia supernova vertical slice now executes:

`M00 accepted -> M01 succeeded -> M02 astronomy/light_curve/data_integration -> M03 draft -> confirmed`

The final contract contains object identity, observation time, band, magnitude/flux alternatives,
source record identity, field-level origins, quality gates, output preferences, immutable hashes,
machine JSON Schema, and a human review view.

## Workflow gates

- M00 rejection or clarification stops before M01.
- M01 ambiguity stops before routing.
- Production defaults to zero capabilities and cannot issue a confirmable specialist contract.
- Only a succeeded formal M02 route with no missing/proposed packs may reach confirmation.
- M03 warnings, unresolved variables, or Schema conflicts remain `needs_review`.
- Confirmation reads a server-issued draft by ID, recomputes integrity hashes, checks the expected
  hash, is atomic across local threads, and is idempotent for the same reviewer.
- Cross-stage checks bind the accepted research goal, compiled problem ID, routing input/decision
  hashes, and contract references so same-run artifacts from another problem cannot be substituted.
- Checkpoints form the ordered causal chain `task.accepted`, `problem.compiled`,
  `routing.completed`, `contract.compiled`, `contract.confirmed`.
- Sensitive/restricted tasks with external models requested are compiled locally when policy
  disables external processing.
- An external M01 compiler is also replaced by the local compiler when its token reservation would
  exceed the accepted M00 model-token allocation.

## Verification

- `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check.ps1`: 122 tests passed with
  91.29% branch-aware repository coverage; all static, security, secret, and dependency checks
  passed.
- Phase 1 workflow and CLI tests cover the confirmed Ia path, production fail-closed behavior,
  M00/M01/M03 stopping gates, privacy fallback, concurrent replay, confirmation failures, public
  output redaction, and checkpoint tampering.
- The repository-wide check runs Ruff, format, strict mypy, pytest with branch coverage, Bandit,
  secret scanning, and dependency vulnerability auditing.
- The Qwen/Bailian adapter is exercised with `httpx.MockTransport`; no real API credential or
  competition endpoint call is claimed by this acceptance record.

## Run the offline demonstration

```powershell
uv run scidatafusion phase1-demo `
  --goal "Integrate multi-source Type Ia supernova light curves into CSV." `
  --confirmed-by "demo-reviewer"
```

The JSON response must report `capability_mode: simulated_demo` and must not contain the research
text or reviewer identity.

## Known deployment boundary

Workflow/artifact storage, confirmation compare-and-set, and the event outbox are currently
in-memory. Production multi-worker deployment also needs authenticated reviewer context and a live
capability health registry. These limits do not weaken the offline Phase 1 contract demonstration,
but they are explicit prerequisites for deployment acceptance.
