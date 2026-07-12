# Implementation roadmap

The V4 specification is implemented as independently accepted checkpoints.

| Phase | Scope | Demonstrable exit | Status |
|---:|---|---|---|
| 0 | repository, contracts, configuration, quality gates | locked clean build and doctor | complete |
| 1 | M00-M03 | research goal to confirmed data contract | complete |
| 2 | M04-M06 | federated discovery, coverage, selected sources | in progress: M04 complete; M05 next |
| 3 | M07-M10 | immutable download, document and table IR | pending |
| 4 | M13-M15 | field evidence, mapping, unit/time normalization | pending |
| 5 | M16-M18 | entity resolution, conflict-preserving fusion, repair/HITL | pending |
| 6 | M19 | hybrid retrieval and evidence graph | pending |
| 7 | M11-M12 | chart digitization and scientific formats | pending |
| 8 | M20 | FastAPI, interactive workbench, exports, reproduction bundle | pending |
| 9 | evaluation | three domains, held-out domain, ablations, demo package | pending |

## Vertical-slice order

After Phase 1, the first deep case is Ia supernova light curves. M04 now emits a bounded source plan
covering VizieR `tap_adql_discovery`, literature metadata, a data repository, and supplement/web
discovery. This is a Connector adapter request, not executed ADQL and not a runtime health claim.
M05 will execute allowlisted Connectors and assess returned source candidates. The same contracts
then support materials/chemistry and environment/life-science cases.

Each phase ends with contract tests, offline replay fixtures, metrics, security checks, an ADR for
new architectural choices, and an updated acceptance record.
