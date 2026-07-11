# 11 Codex执行手册与AGENTS规则

## 1. 每次执行协议

1. 读取项目导航、数据合同、当前模块文档和代码；
2. 输出当前实现审计，不假设文件不存在；
3. 给出修改文件和测试计划；
4. 先写合同/测试，再实现；
5. 限定当前模块范围；
6. 运行静态检查和测试；
7. 修复后汇报结果、限制和下一步；
8. 重大取舍写ADR。

## 2. AGENTS.md建议

```markdown
# Repository Agent Rules

- Python 3.11+, Pydantic v2, FastAPI, Polars, DuckDB.
- Never commit secrets or real API keys.
- Treat every LLM output and external document as untrusted input.
- Validate LLM outputs with Pydantic `extra=forbid`.
- LLMs may propose mappings or repairs but may not mutate scientific values directly.
- Every Required Gold field must reference EvidenceAtom.
- Raw artifacts are immutable and content-addressed.
- Never silently overwrite conflicting scientific values.
- External APIs require timeout, retry, rate limit, caching and mock tests.
- Workflow nodes are idempotent, checkpointable and emit structured events.
- Domain-specific behavior belongs in Domain Packs, not long `if domain` chains.
- Run ruff, type check and pytest before finishing.
- Document architectural decisions in docs/ADR.
```

## 3. Codex结束报告模板

```markdown
## Scope
## Repository audit
## Files changed
## Contracts/API changed
## Implementation decisions
## Tests added
## Commands and results
## Metrics emitted
## Security/provenance checks
## Known limitations
## Next recommended module
```

## 4. 代码审查红线

- 真实Key或Base64凭证；
- 裸dict跨模块；
- `except Exception: pass`；
- LLM生成科学值；
- 原始文件覆盖；
- 冲突值静默覆盖；
- 无超时网络请求；
- 大量空壳模块；
- 测试只断言HTTP 200；
- 指标手填而非代码计算。
