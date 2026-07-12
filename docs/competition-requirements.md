# Competition requirement trace

Source checked on 2026-07-11:
<https://university.aliyun.com/action/tzbjbgs2026>

## Official constraints

- The base model must be from the Qwen family and be called through Alibaba Cloud Model Studio
  (Bailian) or an officially recommended competition tool. Evidence of the call is required.
- The application should find and combine papers, open databases, tables, supplementary files,
  and image or chart data from a research goal.
- It must clean data, align fields, label sources, and emit structured output.
- Chart extraction must explain recognition, extraction, and validation methods.
- Missing values, duplicates, inconsistent units, and axis or legend errors should be detected;
  automatic repair or repair after human advice may earn additional credit.
- The final technical proposal is a PDF of at most 20 pages.
- Direction 1 submission materials include a callable test API and interactive front end,
  representative cases and I/O, structured samples and field descriptions, source and processing
  records, a detailed technical report, and source code. A video of at most 10 minutes is optional.

## Delivery mapping

| Requirement | Planned phase | Verification artifact |
|---|---:|---|
| Qwen/Bailian invocation | 1 | Mock audit trace complete; credentialed redacted invocation proof pending deployment |
| Research goal to data contract | 1 | contract API and golden cases |
| Multi-source discovery | 2 | coverage matrix and replay log |
| Documents, tables, supplements | 3 | Bronze manifest and Silver IR |
| Field evidence and normalization | 4 | EvidenceAtom and transformations |
| Duplicates, conflicts, repair, HITL | 5 | quality and review records |
| Chart/scientific file validation | 7 | calibration and error report |
| Test API, interactive UI, exports | 8 | running application and reproduction bundle |
| Three domains and metrics | 9 | benchmark, ablation, and demo report |
