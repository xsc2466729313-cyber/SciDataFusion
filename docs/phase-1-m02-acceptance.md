# M02 acceptance: domain and task-archetype routing

## Exit criteria

- Routing combines deterministic keyword/relationship evidence with versioned Domain and Task
  Pack registries; a model confidence value is never the sole decision.
- Decisions retain ranked domains/archetypes, evidence, calibrated confidence, registry hash,
  replay key, decision hash, fallback path, and warnings.
- Same input, capability set, and registry snapshot reproduce the same complete decision.
- Low-confidence specialist packs remain proposed while a conservative generic path is used.
- Missing capabilities are explicit; an unavailable specialist pack is never silently enabled.
- The default runtime fails closed with zero capabilities. Tests inject a simulated healthy
  capability set explicitly when exercising formal routes.

## Verification

- `uv run pytest tests/test_routing.py -q --no-cov`: 17 passed.
- Ruff and format checks passed for all 11 M02 source/test files.
- mypy passed for all 11 M02 source/test files.
- Fixtures cover astronomy/Ia light curves, materials/chemistry, environment/life science,
  cross-domain routing, held-out domains, prompt-injection text, registry tampering, missing
  capabilities, replay, strict contracts, and computed metrics.

## Known boundary

Registry capabilities express requirements, not implementation health. M04-M06 will register
real Connector capabilities; until then formal routing is available only in explicit test/demo
contexts and production defaults remain fail-closed.
