# 08 数据平台、API、安全与可观测性

## 1. Bronze/Silver/Gold

- Bronze：原始不可变字节、HTTP元数据、哈希；
- Silver：DocumentIR/TableIR/FigureIR/DatasetIR、Evidence候选；
- Gold：合同对齐、规范化、实体融合、质量通过的视图。

## 2. 存储

| 数据 | 比赛版 | 生产版 |
|---|---|---|
| 任务元数据 | SQLite/PostgreSQL | PostgreSQL |
| 原始文件 | 本地hash目录 | OSS/S3 |
| 分析数据 | Parquet+DuckDB | Lakehouse/对象存储 |
| 向量 | FAISS | Qdrant/pgvector |
| 图 | NetworkX+JSON | Neo4j |
| Trace | JSONL+OTel | OTel后端 |

## 3. API

- `POST /v1/tasks`
- `GET /v1/tasks/{id}`
- `POST /v1/tasks/{id}/confirm-contract`
- `GET /v1/tasks/{id}/sources`
- `GET /v1/tasks/{id}/artifacts`
- `GET /v1/tasks/{id}/data`
- `GET /v1/tasks/{id}/evidence/{evidence_id}`
- `GET /v1/tasks/{id}/quality-issues`
- `POST /v1/tasks/{id}/reviews/{issue_id}`
- `POST /v1/tasks/{id}/cancel`
- `GET /v1/tasks/{id}/exports`

所有写操作支持幂等键；长任务返回202与状态URL。

## 4. 安全

- 密钥通过环境变量/Secret Manager；
- URL下载阻断localhost、RFC1918、link-local和云元数据地址；
- 网络域名按Connector白名单；
- 压缩包防路径穿越和压缩炸弹；
- 文档文本作为不可信输入，防Prompt Injection；
- 不执行论文附件代码；
- 用户数据和日志按隐私等级脱敏。

## 5. 可观测性

Trace层级：Task→Module→Tool/Model/Parser→Record/Issue。Span记录版本、输入输出计数、延迟、费用、缓存、错误码和质量分。

## 6. 费用与限流

百炼模型按模型独立限流，账号下多个Key/空间可能合并计算。实现Token Bucket、平滑并发、Retry-After、预算预估和实际费用归集。
