# Multi-channel search acceptance

Version: 1.3.0

- [x] Qwen plans strict channel-specific queries.
- [x] Deterministic fallback covers Google Web, Google Scholar, and arXiv.
- [x] SerpApi selects the requested Google engine per query.
- [x] arXiv uses an allowlisted HTTPS endpoint, three-second spacing, retry, cache, and hardened XML.
- [x] Results are round-robin merged, URL-deduplicated, and capped at 20.
- [x] The Chinese workbench shows channel labels, result counts, and call proof.
- [x] Mock tests cover success, retry, cache, hostile payload, offline, and partial-failure paths.
