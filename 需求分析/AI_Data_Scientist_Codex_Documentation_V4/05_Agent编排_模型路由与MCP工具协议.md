# 05 Agent编排、模型路由与MCP工具协议

## 1. Agent角色

| 角色 | 输出 | 不允许做的事 |
|---|---|---|
| Specifier | ProblemSpec/DataContract候选 | 猜科学数值 |
| Planner | Search/Parse/Repair计划 | 调用未注册工具 |
| Executor | 执行确定性工具 | 自行改合同 |
| Verifier | 质量门和证据校验 | 只用自评置信度放行 |
| Repairer | 白名单修复动作 | 直接编造替代值 |

## 2. Durable DAG

工作流节点要“小而清晰”：每个节点一件事，状态存原始结构而不是格式化文本。支持暂停、恢复、时间旅行、局部重跑和人工中断。

## 3. 模型路由

禁止在源代码硬编码具体模型快照；通过逻辑角色配置：

```yaml
models:
  planner: ${PLANNER_MODEL_ID}
  fast_classifier: ${FAST_MODEL_ID}
  critic: ${CRITIC_MODEL_ID}
  vision: ${VISION_MODEL_ID}
  ocr: ${OCR_MODEL_ID}
  embedding: text-embedding-v4
  rerank: qwen3-rerank
  multimodal_embedding: ${MULTIMODAL_EMBEDDING_MODEL_ID}
  multimodal_rerank: ${MULTIMODAL_RERANK_MODEL_ID}
```

正式调用前从百炼模型列表校验可用性；演示固定模型快照，开发可使用Alias。

## 4. 置信度融合

模型自评只是一项特征。最终置信度建议综合：规则一致、Schema验证、来源可信、跨源一致、解析质量、模型一致、人工状态。

```text
C = w_rule*C_rule + w_schema*C_schema + w_source*C_source
  + w_cross*C_cross + w_parse*C_parse + w_model*C_model
```

权重必须在验证集上校准，不能拍脑袋后当成概率。

## 5. MCP/Capability Registry

工具统一声明：名称、版本、输入Schema、输出Schema、权限、成本、速率、适用领域、幂等性和副作用。MCP可作为外部工具互联协议，但内部仍需要Capability Registry控制。

```yaml
name: search.openalex
version: 1.0.0
input_schema: SearchQuery
output_schema: SourceCandidateList
permissions: [network:openalex.org]
cost_model: request
idempotent: true
timeout_seconds: 20
domains: [generic]
```

## 6. Context Engineering

- System：不可变安全和科学规则；
- Task：当前合同和节点目标；
- Retrieved Evidence：可追溯、限长、分隔；
- Tool results：结构化，不拼接成自然语言大段；
- Memory：只有通过准入的规则/经验；
- Output schema：严格JSON。

## 7. 模型失败策略

非法JSON→本地解析/一次结构修复→低级模型/模板回退→人工；限流→队列和指数退避；大上下文→证据压缩和分层检索；视觉失败→OCR/解析器或人工。
