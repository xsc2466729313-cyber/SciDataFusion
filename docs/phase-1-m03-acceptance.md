# M03 acceptance: dynamic scientific data-contract compilation

## Exit criteria

- A succeeded formal M02 route composes versioned domain/task Schema Packs into one immutable
  `ScientificDataContract` and `CanonicalSchema`; every other route remains `needs_review`.
- Schema Pack references are bound to the exact M02 source-pack content hash. Registry files are
  size-limited, strict-schema validated, and protected by a canonical SHA-256 manifest hash.
- Required fields retain field-level origins. Product-level intent such as `light curves` maps to
  the complete task-pack field set without inventing a synthetic scientific measurement.
- Unresolved variables remain required strings with no guessed dimension, range, conversion, or
  target unit and block contract confirmation.
- Compatible definitions merge through validated reconstruction. Incompatible definitions keep
  both complete alternatives in a blocking `SchemaConflict`; no incoming definition is discarded.
- Explicit output preferences, conditions, temporal/spatial scopes, and upstream assumptions are
  frozen with evidence references and participate in the contract content hash.
- Confirmation recomputes contract, schema, and identifier hashes, requires optimistic hash
  matching, accepts only drafts, and returns the same confirmation for an idempotent retry.
- Contracts provide deterministic JSON Schema, a human review view, quality gates, computed
  metrics, content hashes, structured events, and field plus metadata version diffs.

## Verification

- `uv run pytest tests/test_contract_compiler.py -q --no-cov`: 18 passed.
- `uv run pytest -q`: 105 passed; branch-aware repository coverage 92.15%.
- Ruff, format, and strict mypy checks passed for the M03 contracts, compiler, registry, and tests.
- Fixtures cover the Ia light-curve golden path, explicit Parquet/time scope, replay, confirmation,
  tampering, unsupported routing, unresolved variables, multi-pack conflicts, derivation cycles,
  registry corruption, strict linkage, machine schema, and contract diffs.

## Metric interpretation

The module reports field, required-field, conflict, and warning counts from result artifacts. The
tests verify contract validity and fixture behavior; they do not claim the specification's corpus-
level Schema Induction F1 or conflict-recall targets. Those require the later evaluation corpus.

## Known boundary

M03 caches compilation and confirmation records in memory. A durable artifact repository and
cross-process compare-and-set confirmation belong to the workflow persistence checkpoint. Formal
routing also remains available only when a caller explicitly supplies a healthy capability set;
the production router default continues to fail closed.
