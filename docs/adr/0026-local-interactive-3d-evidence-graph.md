# ADR 0026: Local interactive 3D evidence graph

Status: Accepted for v1.1.0.

## Context

The workbench previously projected the M19 evidence graph onto a static circular 2D canvas. Users
could not inspect a node, follow a relationship, filter dense categories, or understand the graph's
depth without reading tables separately. The portable Windows release must remain fully usable
offline and must not load executable code from a CDN.

## Decision

1. Bundle Three.js 0.185.1 and its MIT license inside `scidatafusion.web`; serve the exact module and
   core files from same-origin FastAPI asset routes.
2. Extend the workbench projection with each node's existing `source_id` and every edge's existing
   `evidence_refs`. The UI does not infer, repair, or mutate scientific values.
3. Render a deterministic force-directed 3D presentation with rotate, zoom, node drag/pin, hover,
   click inspection, category filters, layout pause, and camera reset.
4. Keep raw labels and identifiers in the API. Chinese display labels are presentation-only aliases.
5. Preserve a readable WebGL failure state. The rest of the workbench remains available if 3D
   rendering cannot initialize.
6. Verify desktop and mobile layouts with Playwright, including WebGL pixel reads, interaction,
   browser errors, and page-level overflow.

## Consequences

- The portable package grows by about 0.8 MB for the local Three.js modules.
- GPU acceleration or a software WebGL implementation is required for the 3D surface.
- Graph positions are intentionally presentation state and are not part of evidence identity,
  topology hashes, retrieval scores, or delivery artifacts.
