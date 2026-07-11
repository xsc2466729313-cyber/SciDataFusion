# V4 specification audit

The supplied documentation is the product authority, but the following inconsistencies are tracked
before implementation so they do not silently change module scope.

## Confirmed issues

1. `00_项目文档导航与执行顺序.md` refers to evaluation as document 08 and the Codex manual as
   document 10. The actual files are 09 and 11.
2. The README suggests implementing M07-M12 together, while the phase roadmap defers M11/M12 until
   after M19 and Domain Packs. The phased roadmap is used because it has explicit exits.
3. The combined document contains 21 broken `../modules/...` links. Split module documents remain
   readable and are used as the implementation source.
4. `MANIFEST.json` does not correctly describe its own current size/hash, although the other 43
   extracted files match the ZIP contents.
5. GraphRAG, scientific formats, and RO-Crate appear across both P0 and P1 descriptions. They are
   implemented only at the explicit roadmap phase and are not Phase 0/1 acceptance blockers.
6. The original matrix does not make the direction-specific interactive UI and callable test API
   submission gate prominent. The official-page trace in `competition-requirements.md` does.
7. The ZIP stores UTF-8 path bytes without setting the ZIP UTF-8 filename flag. The final delivery
   archive must be rebuilt and extraction-tested on Windows and Linux.

## Decisions still due

- Durable workflow engine and metadata-store production upgrade path.
- Final interactive frontend framework.
- Four initial connector implementations and their credentials/quotas.
- Exact three deep-domain benchmark cases and held-out domain.
- Model snapshots, cost budget, latency objective, and golden-set acquisition plan.

These are decided in the phase where their evidence is available, not guessed in Phase 0.
