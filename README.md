# SciDataFusion 科学数据智能工作台

输入一个研究方向，系统会自主规划检索重点，从 Google、Google Scholar、arXiv 和开放数据站点发现论文与多源材料，完成受控下载、解析、字段对齐、证据绑定、质量检查和结构化交付。缺少真实证据时不会生成科学数值，也不会把内置样例冒充当前主题结果。

![SciDataFusion 中文工作台](docs/assets/workbench-v1.4.png)

## 主要能力

- **主题自主探索**：用户只需描述想研究什么，AI 自动生成检索式、候选数据源、目标字段和质量检查。
- **多源数据发现**：组合网页、学术论文、预印本、开放数据库、CSV/TSV/JSON、附件、图表和科学文件。
- **真实数据整合**：联网获得的 CSV、TSV、JSON 会在哈希校验后显示真实行列；AI 只辅助对齐字段名，未确认列保留原名，并可下载逐单元格可追溯的多源证据 CSV。
- **可追溯整合**：原始文件按内容寻址，字段保留来源、位置、转换和 EvidenceAtom，冲突值不会被静默覆盖。
- **中文交互工作台**：展示研究进度、来源覆盖、证据质量、交付文件，以及可拖拽、缩放、点击查看中文关系详情的 3D 知识图谱。
- **两种运行方式**：单机模式开箱即用；平台模式使用 PostgreSQL、Redis、Celery 和 Chroma 支撑持久化任务与证据向量索引。

## Docker 一键运行

需要 Docker Desktop。首次启动：

```powershell
Copy-Item compose.env.example .env
docker compose --env-file .env up --build -d
```

打开 [http://127.0.0.1:8080](http://127.0.0.1:8080)。默认可以离线体验；联网研究可直接在页面“配置”中填写：

- 阿里云百炼 API Key
- SerpApi Key
- 百炼 Base URL，默认 `https://dashscope.aliyuncs.com/compatible-mode/v1`

页面不会回显密钥。Docker 模式写入本机持久配置卷，Windows 模式写入程序数据目录中的忽略配置文件，重启后继续生效；工作台只绑定 `127.0.0.1`，配置接口拒绝远程写入。停止服务：

```powershell
docker compose down
```

## Windows 直接下载

在 [GitHub Releases](https://github.com/xsc2466729313-cyber/SciDataFusion/releases/latest) 下载 `SciDataFusion-1.7.0-windows-x64.zip`，完整解压后双击 `SciDataFusion.exe`。便携版包含 Python 运行环境和中文 React 页面，无需安装 Python、Node.js 或 Git。

## 源码开发

需要 Python 3.11+、[uv](https://docs.astral.sh/uv/)、Node.js 24+。

```powershell
uv sync --python 3.11 --group dev --extra scientific --extra platform
npm.cmd --prefix frontend ci
Start-Process powershell -ArgumentList '-NoProfile','-Command','uv run uvicorn scidatafusion.api:app --host 127.0.0.1 --port 8000'
npm.cmd --prefix frontend run dev
```

前端地址为 [http://127.0.0.1:5173](http://127.0.0.1:5173)，API 文档为 [http://127.0.0.1:8000/api/docs](http://127.0.0.1:8000/api/docs)。

## 输出内容

每个研究任务会形成来源清单、原始产物哈希、可审核字段映射、逐单元格证据长表、质量问题、证据关系图和复现元数据。证据 CSV 可直接用于筛选、透视和后续分析；只有当前主题的真实文件通过语义、单位、冲突和完整性质量门后，才会开放正式 Gold CSV/Parquet。

默认演示使用 VizieR `J/AJ/154/211/OptPhot` 中 SN 2004dt 的 8 条真实 B 波段测光记录，字段定义可在 [VizieR 官方目录](https://cdsarc.cds.unistra.fr/viz-bin/ReadMe/J/AJ/154/211?format=html&tex=true) 核验。

## 技术结构

| 层 | 实现 |
| --- | --- |
| 交互界面 | React、TypeScript、Vite、Three.js 生态 3D 图谱 |
| AI 服务 | FastAPI、Pydantic v2、LangGraph，可选 LangChain/LlamaIndex 视图 |
| 数据处理 | Polars、DuckDB、scikit-learn，可选 PyTorch 向量校验 |
| 平台服务 | PostgreSQL、Celery、Redis、Chroma |
| 部署发行 | Docker Compose、Nginx、PyInstaller Windows 便携包 |

平台能力均采用可选依赖。默认镜像保持轻量；需要 PyTorch 时在 `.env` 设置 `SCIDATA_INSTALL_TORCH=true` 后重建。

## 质量与安全

```powershell
powershell -ExecutionPolicy Bypass -File scripts/check.ps1
```

门禁包含 Ruff、mypy、pytest、Bandit、秘密扫描和依赖漏洞检查。外部文档和模型输出都按不可信输入处理；模型只能提出检索、映射或修复建议，不能直接写入或发明科学值。

部署边界见 [M26 ADR](docs/adr/0033-deployable-ai-service-platform.md)，当前主题解析见 [M27 ADR](docs/adr/0034-current-topic-structured-preview.md)，字段映射与证据表边界见 [M28 ADR](docs/adr/0035-reviewable-field-mapping-and-evidence-export.md)。中文任务诊断、本机配置和证据关系语义见 [M29 ADR](docs/adr/0036-local-configuration-and-chinese-evidence-graph.md) 与 [M29 验收清单](docs/chinese-workbench-reliability-acceptance.md)。
