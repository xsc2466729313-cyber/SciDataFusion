# M06 acceptance: candidate coverage and source selection

## Exit criteria

- M06 validates the confirmed M03 contract, M04 plan, and M05 Connector result hashes before use
  and rejects cross-task, cross-run, cross-version, contract, plan, candidate, and output tampering.
- Strict Pydantic contracts cover `SelectedSourceSet`, `CoverageReport`, `SearchGapSet`, retained
  progress, metrics, and `selection.completed`. Metrics, status, warnings, coverage ratios,
  diversity, primary-source state, cumulative marginal gains, and event references are derived.
- Candidate coverage is projected only from non-unknown M05 claims that meet explicit confidence
  thresholds and map through an exact M04 coverage cell. Evidence IDs, basis, confidence, source
  IDs, and contract source types remain attached. The report labels all such output candidate-only.
- The selector chooses one representative per replica group, assigns one diversity category per
  selected candidate, retains a primary source when available, and balances Required/optional
  fields, source types, categories, locator readiness, license clarity, and deterministic source
  assessment under source-count and byte-reservation limits.
- Every selected source has explicit reason codes, evidence, immutable candidate hash, download
  locators, readiness, byte reservation, access state, and a conservative license decision.
- The coverage report includes every confirmed field, entity key, quality gate, selection
  constraint, M04 coverage cell, and acceptable contract source type. Uncertainty and uncovered
  entries are never removed from the denominator.
- Missing or uncertain Required fields, unsatisfied gates, unverified scope, a missing primary
  source, insufficient categories/types, unresolved licenses, no candidates, and exhausted budget
  are represented as structured gaps. Searchable gaps receive deterministic directives.
- The retained progress snapshot reproduces the existing hard-limit and evidence-saturation stop
  decision. Coverage saturation requires no blocking gaps plus the configured two recent low-gain
  rounds; one successful first round still continues.
- Canonical input, selected-set, report, gap-set, and output hashes detect body tampering. Identical
  requests replay one immutable process-local result.

## Offline Ia acceptance

`phase2-select-demo` runs M00-M06 using the packaged no-network Ia fixture. M05 supplies five
candidate sources. M06 selects three distinct replica groups assigned to literature metadata, a
data repository, and a domain database; at least one selected candidate is marked primary.

The fixture produces:

- Required-field candidate coverage: `1.0`;
- entity-key candidate coverage: `1.0`;
- acceptable-source-type candidate coverage: `1.0` across open database, paper table, supplement,
  and figure candidate classes;
- selected source categories: `3`;
- reserved unknown-size download budget: `3,000,000` bytes;
- blocking gaps: one record-level scope constraint and three license reviews;
- status: `partial`;
- first-round marginal Required-field gain: `1.0`;
- decision: `continue_search`, emitted as `selection.completed`;
- confirmed-live and unknown-network attempts: both zero in the upstream offline Connector run.

The `partial` status is evidence of a correct boundary, not a fixture failure. Discovery metadata
cannot prove the record-level Type Ia condition or redistribution permission for these three
selected candidates. M07 and later parsing/review stages must resolve those gaps.

## Verification

- `uv run pytest tests/test_selection_contracts.py tests/test_selection_integrity.py -q --no-cov`
  covers strict contracts, cross-run and upstream-hash rejection, exact matrix projection,
  idempotency, budget exhaustion, stable artifact hashes, source/body tampering, and forged stop
  decisions.
- `uv run pytest tests/test_cli.py -q --no-cov` verifies the safe M00-M06 command summary and
  confirms that it omits the research goal, reviewer identity, source URLs, untrusted excerpts, and
  hostile fixture text.
- `uv run scidatafusion phase2-select-demo --goal "Study Type Ia supernova light curves using
  multi-source data integration into CSV." --confirmed-by "demo-reviewer"` reproduces the metrics
  above without external network access.
- The repository gate runs Ruff, format checking, strict mypy, branch-aware pytest, Bandit, secret
  scanning, and dependency auditing.

## Metric interpretation

The reported coverage ratios measure evidence-backed discovery candidates against one confirmed
contract. They do not measure parsed-row completeness, scientific correctness, or final dataset
quality. The fixture is not a judged retrieval corpus, so it cannot establish Source Recall@20,
nDCG@20, or an invalid-download rate. Those targets require versioned relevance judgments, real
download outcomes, sample counts, baselines, and confidence intervals.

## Known boundary

M06 performs no network or model call and downloads no source. Candidate file sizes are unknown and
use an explicit reservation rather than an invented actual size. Scope claims stay unverified
because M05 has no structured record-level scope evidence. The greedy selector is deterministic
and explainable but is not a proof of global set-cover optimality. Process-local caching is not a
durable workflow store. M07 must resolve locators under controlled network policy, preserve bytes,
enforce license decisions, and recompute coverage after parsing.
