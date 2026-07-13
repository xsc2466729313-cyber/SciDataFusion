# M16 acceptance: conservative entity resolution and duplicate detection

## Status

Accepted on 2026-07-13 for the first exact stable-identifier offline slice. This is not acceptance
of fuzzy, probabilistic, coordinate/time-neighborhood, alias-graph, external-registry, LLM, or
benchmark-backed entity resolution.

## Definition of Done

- [x] Strict frozen Pydantic v2 contracts with `extra="forbid"` cover request, policy, runtime,
  resolution evidence, entity clusters, duplicate groups, sets, metrics, result, and event payload.
- [x] M16 re-verifies the exact M13-M15 request/result/event chain, contract, nested hashes,
  evidence references, and Bronze bytes before execution or replay.
- [x] Entity blocking uses only contract entity keys that M15 explicitly marks eligible. Key values
  are represented in M16 artifacts by hashes and are never guessed, repaired, or exposed by CLI.
- [x] Candidate generation is bucketed by exact entity fingerprint and never constructs full
  `O(n^2)` pairs. Candidate and total-pair metrics derive from bucket cardinalities.
- [x] Every automatic merge records exact features, evidence, method, score, threshold, and member
  lineage. A singleton is not counted as an automatic merge.
- [x] Duplicate groups are separate from entity clusters and require exact equality of every
  M16-eligible field fingerprint within the same entity cluster.
- [x] Missing or blocked entity keys remain explicit unresolved records and cannot be fuzzy- or
  model-merged in this slice.
- [x] Complete results are immutable, checkpointable, single-flight, cancellation isolated,
  tamper evident, and emit one causally linked `entity.resolved` event.
- [x] No LLM decision, fuzzy auto-merge, network access, scientific-value mutation, Gold write, or
  cost occurs.
- [x] Architecture, limitations, metrics, verification commands, and offline evidence are recorded.
- [x] Final repository gates and measured totals are recorded after the complete M16 run.

## Offline Ia evidence

`phase5-resolve-demo` executes the packaged M00-M16 chain without external services. The one M15
record has an eligible `object_id` key, so M16 creates one exact-key resolution-evidence record and
one singleton entity cluster. There are zero candidate pairs, automatic merges, and duplicate
groups. The cluster is eligible for M17, but the aggregate remains `partial` because M15 retains
unresolved source-unit and time-scale issues.

This is an honest single-record functional proof. It cannot establish Entity Resolution F1,
deduplication F1, false-merge rate, candidate-pair recall, or manual-review rate. Those metrics need
a labeled multi-source, multi-record benchmark with same-name/different-entity, alias, identifier
conflict, and transitive-inconsistency cases.

The CLI contains only aggregate decisions, metrics, opaque identifiers, and hashes. It omits entity
key names and values, normalized scientific values, evidence content, goal text, and reviewer
identity.

## Verification

```powershell
uv run pytest tests/test_entity_resolution_service.py tests/test_cli.py -q --no-cov
uv run scidatafusion phase5-resolve-demo `
  --goal "Study Type Ia supernova light curves" --confirmed-by "m16-acceptance"
powershell -ExecutionPolicy Bypass -File scripts/check.ps1
```

The final repository gate completed with 595 passing tests and 90.02% total coverage. Ruff and
format checks passed across 180 files, mypy passed across 179 source files, Bandit reported no
issues, the secret scan passed across 620 files, and the dependency audit reported no known
vulnerabilities.

## Known boundaries

- The packaged Ia route currently yields one normalized record, so no production merge or
  duplicate decision occurs. Multi-record helper tests verify decision invariants but are not a
  substitute for a labeled integration corpus.
- Exact identifier comparison is case-sensitive because M16 cannot normalize identifiers. Domain
  Pack identifier normalization and alias graphs need evidence-backed M15 rules.
- Coordinate/time proximity, attribute similarity, probabilistic scoring, blocking recall, and
  transitive closure consistency are deferred.
- External identifier registries and semantic model comparisons are disabled; future adapters must
  satisfy allowlist, timeout, retry, rate-limit, cache, strict-validation, and Mock-test controls.
- M16 does not select conflicting scientific values, repair records, or write Gold. M17 owns
  conflict-preserving fusion and M18 owns quality audit/repair.
