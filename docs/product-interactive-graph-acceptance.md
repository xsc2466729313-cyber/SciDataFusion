# Interactive evidence graph acceptance

## Definition of Done

- [x] The graph uses the real workbench nodes and edges and displays 87 nodes and 130 relationships.
- [x] Node details expose the existing source ID, node ID, trust state, degree, related nodes, edge
  kind, and evidence-reference count.
- [x] Users can orbit, zoom, drag and pin nodes, select nodes, filter all six graph categories, pause
  layout, and reset the camera.
- [x] Node and relation labels are presented in Chinese while immutable identifiers remain intact.
- [x] Three.js 0.185.1 is bundled locally with its license and works in the portable offline build.
- [x] Workbench graph contracts retain strict Pydantic validation and forbid extra fields.
- [x] Playwright verification passes at 1440x1000 and 390x844 with no horizontal overflow or browser
  error. WebGL pixel checks observe at least 50 distinct color buckets; filtering evidence nodes
  reduces the active graph from 87 to 11 nodes, and desktop node dragging records a pinned node.
- [x] Ruff, mypy, pytest, Bandit, the secret scan, and dependency audit pass before release.

The graph is a presentation of validated topology. Its force layout and Chinese display aliases do
not change scientific values, evidence references, source identities, graph hashes, or quality-gate
decisions.
