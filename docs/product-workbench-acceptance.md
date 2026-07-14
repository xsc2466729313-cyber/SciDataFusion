# Connected Chinese scientific-data workbench acceptance

## Product scope

The first screen is the working application, not a checkpoint list or marketing page. It presents
one connected flow from a Chinese research goal through sources, immutable artifacts, parsing,
field alignment, normalization, fusion, evidence, quality review, visualization, and delivery.

## Acceptance checklist

- [x] All visible product navigation and primary content is valid UTF-8 Chinese.
- [x] Six business stages replace module identifiers in the primary workflow visualization.
- [x] The source view exposes selection rank, source families, categories, field coverage,
  licensing, readiness, and scores.
- [x] The parsing view exposes formats, MIME types, parser routes, hashes, field raw/normalized/
  fused values, mapping scores, evidence counts, and FITS DatasetIR metrics.
- [x] Field evidence exposes source table position, byte range, method, confidence, and identity.
- [x] Quality views expose every gate, threshold, score, issue, action, and retrieval channel score.
- [x] Canvas visualizations use actual M11 chart points and M19 graph nodes/edges.
- [x] Delivery lists every package artifact and retains quality-gated CSV behavior.
- [x] Desktop 1280x720 and mobile 390x844 browser runs render all six views without page-level
  horizontal overflow. The light-curve and evidence graph contain drawn data, the run control
  completes and re-enables, and the browser console has no application warning or error.

The M21 source view additionally shows the offline/online segmented control, runtime readiness,
SerpApi/Qwen invocation proof, and a bounded live-source table. With no local secrets configured,
the final acceptance server correctly disables online selection and reports zero external calls;
the online-rendering data contract and API path are covered by Mock end-to-end tests.

M22 adds a sixth Chinese `联网配置` view with the effective search engine, locale, query/result
bounds, endpoint host, planner and assessment models, and credential readiness. The view also
provides a local edit form: it writes allowlisted values into `.env`, applies them immediately,
preserves unrelated settings, rejects remote writes, and never renders or returns a secret value.
Online runs show every planned query, its purpose, expected evidence type, execution status, and
result count.
