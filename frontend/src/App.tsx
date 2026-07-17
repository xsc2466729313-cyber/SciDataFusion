import { lazy, Suspense, useEffect, useState } from "react";
import {
  Activity,
  Boxes,
  CheckCircle2,
  CircleAlert,
  Database,
  Download,
  ExternalLink,
  FileSearch,
  FlaskConical,
  KeyRound,
  Network,
  Play,
  RefreshCw,
  Search,
  Settings,
  ShieldCheck,
  TableProperties
} from "lucide-react";
import { api } from "./api";
import type {
  GraphNode,
  OnlineConfiguration,
  PlatformStatus,
  ResearchJob,
  StructuredDatasetPreview,
  TabKey,
  WorkbenchSnapshot
} from "./types";

const tabs: Array<{ key: TabKey; label: string; icon: typeof Activity }> = [
  { key: "overview", label: "研究总览", icon: Activity },
  { key: "sources", label: "来源与样本", icon: Database },
  { key: "evidence", label: "证据与字段", icon: Network },
  { key: "delivery", label: "质量与交付", icon: ShieldCheck },
  { key: "settings", label: "联网配置", icon: Settings }
];

const nodeColors: Record<string, string> = {
  evidence: "#0f766e",
  field: "#2563a4",
  issue: "#b64132",
  gate: "#b46b08",
  source: "#5c6f68"
};

const EvidenceGraph = lazy(() => import("./Graph3D"));

function App() {
  const [activeTab, setActiveTab] = useState<TabKey>("overview");
  const [platform, setPlatform] = useState<PlatformStatus | null>(null);
  const [snapshot, setSnapshot] = useState<WorkbenchSnapshot | null>(null);
  const [goal, setGoal] = useState("研究 Ia 型超新星光变曲线，自动发现论文、开放数据与机器可读表格");
  const [mode, setMode] = useState<"offline" | "online">("offline");
  const [job, setJob] = useState<ResearchJob | null>(null);
  const [selectedNode, setSelectedNode] = useState<GraphNode | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const [platformResult, workbenchResult] = await Promise.all([
        api.platform(),
        api.workbench()
      ]);
      setPlatform(platformResult);
      setSnapshot(workbenchResult);
      setGoal(workbenchResult.research_goal);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "工作台加载失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  useEffect(() => {
    if (!job || !["queued", "running"].includes(job.status)) return;
    const timer = window.setInterval(async () => {
      try {
        const current = await api.job(job.job_id);
        setJob(current);
        if (current.status === "succeeded") setSnapshot(await api.workbench());
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : "任务状态读取失败");
      }
    }, 1000);
    return () => window.clearInterval(timer);
  }, [job]);

  const runResearch = async () => {
    if (goal.trim().length < 10) {
      setError("研究方向至少需要 10 个字符");
      return;
    }
    setError(null);
    try {
      setJob(await api.submitJob(goal, mode));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "任务提交失败");
    }
  };

  return (
    <div className="app-shell">
      <Sidebar active={activeTab} onChange={setActiveTab} platform={platform} />
      <main className="main-area">
        <header className="topbar">
          <div>
            <div className="eyebrow">SCIENTIFIC DATA INTELLIGENCE</div>
            <h1>SciDataFusion 科学数据工作台</h1>
          </div>
          <div className="header-status">
            <span className="status-dot" />
            {platform?.mode === "celery" ? "分布式服务" : "本地服务"}
          </div>
        </header>

        <section className="research-command" aria-label="新建研究任务">
          <div className="goal-input">
            <label htmlFor="research-goal">我想研究什么</label>
            <textarea
              id="research-goal"
              value={goal}
              onChange={(event) => setGoal(event.target.value)}
              rows={2}
            />
          </div>
          <div className="mode-control">
            <span>运行模式</span>
            <div className="segmented">
              <button className={mode === "offline" ? "active" : ""} onClick={() => setMode("offline")}>
                离线复现
              </button>
              <button className={mode === "online" ? "active" : ""} onClick={() => setMode("online")}>
                联网探索
              </button>
            </div>
          </div>
          <button className="primary-action" onClick={() => void runResearch()} disabled={job?.status === "running"}>
            {job?.status === "running" || job?.status === "queued" ? <RefreshCw className="spin" /> : <Play />}
            {job?.status === "running" ? "正在分析" : job?.status === "queued" ? "等待执行" : "开始分析"}
          </button>
        </section>

        {(error || job) && <RunBanner error={error} job={job} />}

        <div className="content-area">
          {loading && <LoadingState />}
          {!loading && snapshot && activeTab === "overview" && (
            <Overview snapshot={snapshot} selectedNode={selectedNode} onNodeSelect={setSelectedNode} />
          )}
          {!loading && snapshot && activeTab === "sources" && <Sources snapshot={snapshot} />}
          {!loading && snapshot && activeTab === "evidence" && <Evidence snapshot={snapshot} />}
          {!loading && snapshot && activeTab === "delivery" && <Delivery snapshot={snapshot} />}
          {!loading && activeTab === "settings" && <Configuration />}
        </div>
      </main>
    </div>
  );
}

function Sidebar({ active, onChange, platform }: { active: TabKey; onChange: (tab: TabKey) => void; platform: PlatformStatus | null }) {
  return (
    <aside className="sidebar">
      <div className="brand-mark"><FlaskConical /><span>SF</span></div>
      <nav aria-label="工作台导航">
        {tabs.map(({ key, label, icon: Icon }) => (
          <button key={key} className={active === key ? "active" : ""} onClick={() => onChange(key)} title={label}>
            <Icon /><span>{label}</span>
          </button>
        ))}
      </nav>
      <div className="sidebar-runtime">
        <Boxes />
        <div><strong>{platform?.components.filter((item) => item.status === "ready").length ?? 0}</strong><span>能力已就绪</span></div>
      </div>
    </aside>
  );
}

function RunBanner({ error, job }: { error: string | null; job: ResearchJob | null }) {
  const failed = Boolean(error) || job?.status === "failed";
  return (
    <div className={`run-banner ${failed ? "error" : ""}`}>
      {failed ? <CircleAlert /> : job?.status === "succeeded" ? <CheckCircle2 /> : <RefreshCw className="spin" />}
      <strong>{error ? "请求未完成" : job?.status === "succeeded" ? "研究任务已完成" : job?.status === "failed" ? "研究任务未完成" : "研究任务执行中"}</strong>
      <span>{error ?? (job ? `任务 ${job.job_id.slice(0, 14)} · ${job.status}` : "")}</span>
    </div>
  );
}

function Overview({ snapshot, selectedNode, onNodeSelect }: { snapshot: WorkbenchSnapshot; selectedNode: GraphNode | null; onNodeSelect: (node: GraphNode) => void }) {
  return (
    <>
      <section className="section-block">
        <div className="section-heading"><div><h2>研究进展</h2><p>{snapshot.retrieval_query}</p></div><QualityBadge snapshot={snapshot} /></div>
        <div className="stage-strip">
          {snapshot.stages.map((stage, index) => (
            <div className={`stage ${stage.status}`} key={stage.key} title={stage.detail}>
              <span>{index + 1}</span><div><strong>{stage.label}</strong><small>{stage.primary_count} {stage.count_label}</small></div>
            </div>
          ))}
        </div>
      </section>

      <div className="metric-grid">
        <Metric icon={Database} value={snapshot.sources.length} label="有效来源" detail="论文、数据仓库与附件" />
        <Metric icon={FileSearch} value={snapshot.artifacts.length} label="解析对象" detail="内容寻址并保留原始文件" />
        <Metric icon={Network} value={snapshot.evidence.length} label="证据原子" detail="全部可回溯到源位置" />
        <Metric icon={TableProperties} value={snapshot.quality_score.toFixed(2)} label="质量分" detail={snapshot.formal_gold_available ? "正式数据可交付" : "保留待审问题"} />
      </div>

      <section className="graph-workspace">
        <div className="graph-panel">
          <div className="panel-heading"><div><h2>交互式证据知识图谱</h2><p>拖拽旋转 · 滚轮缩放 · 点击节点查看证据</p></div><span>{snapshot.graph_nodes.length} 节点 · {snapshot.graph_edges.length} 关系</span></div>
          <Suspense fallback={<div className="graph-loading"><RefreshCw className="spin" />正在加载三维图谱</div>}>
            <EvidenceGraph nodes={snapshot.graph_nodes} edges={snapshot.graph_edges} onSelect={onNodeSelect} colors={nodeColors} />
          </Suspense>
          <div className="graph-legend">{Object.entries(nodeColors).map(([kind, color]) => <span key={kind}><i style={{ backgroundColor: color }} />{kind}</span>)}</div>
        </div>
        <aside className="node-inspector">
          <h3>节点详情</h3>
          {selectedNode ? (
            <dl><dt>名称</dt><dd>{selectedNode.label}</dd><dt>类型</dt><dd>{selectedNode.kind}</dd><dt>来源</dt><dd>{selectedNode.source_id}</dd><dt>可信状态</dt><dd>{selectedNode.trusted ? "已验证" : "待验证"}</dd><dt>节点标识</dt><dd className="mono">{selectedNode.node_id}</dd></dl>
          ) : <div className="empty-inspector"><Network /><p>点击图谱中的节点查看来源、类型与可信状态</p></div>}
        </aside>
      </section>

      <section className="section-block blueprint">
        <div className="section-heading"><div><h2>{snapshot.research_blueprint.topic_title}</h2><p>{snapshot.research_blueprint.research_summary}</p></div><span className="subtle-badge">自主研究蓝图</span></div>
        <div className="blueprint-grid">
          <ListColumn title="证据检索重点" items={snapshot.research_blueprint.evidence_priorities} />
          <ListColumn title="多元数据来源" items={snapshot.research_blueprint.source_types} />
          <ListColumn title="候选结构化字段" items={snapshot.research_blueprint.candidate_fields} />
          <ListColumn title="目标成果" items={snapshot.research_blueprint.target_outputs} />
        </div>
      </section>
    </>
  );
}

function Sources({ snapshot }: { snapshot: WorkbenchSnapshot }) {
  const mapping = snapshot.online_field_mapping;
  return (
    <section className="section-block">
      <div className="section-heading"><div><h2>来源与样本</h2><p>统一展示检索渠道、许可、字段覆盖和下载状态</p></div><span>{snapshot.sources.length} 个来源 · {snapshot.artifacts.length} 个对象</span></div>
      <div className="table-wrap"><table><thead><tr><th>排名</th><th>来源</th><th>类型</th><th>字段覆盖</th><th>许可</th><th>状态</th><th>评分</th></tr></thead><tbody>
        {snapshot.sources.map((source) => <tr key={source.candidate_id}><td>{source.rank}</td><td><strong>{source.source_names.join(" · ")}</strong><small className="mono">{source.candidate_id}</small></td><td>{source.categories.join(" / ")}</td><td>{source.covered_fields.join("、") || "待解析"}</td><td>{source.license_status}</td><td><span className="table-status">{source.download_status}</span></td><td>{source.score.toFixed(2)}</td></tr>)}
      </tbody></table></div>
      <div className="artifact-band">
        {snapshot.artifacts.slice(0, 12).map((artifact) => {
          const content = <><FileSearch /><div><strong>{artifact.format.toUpperCase()}</strong><span>{artifact.media_type}</span></div><span>{formatBytes(artifact.size_bytes)}</span><code>{artifact.sha256.slice(0, 12)}</code>{snapshot.topic_data_status === "live_discovery" && <Download />}</>;
          return snapshot.topic_data_status === "live_discovery"
            ? <a className="artifact-row" key={artifact.object_id} href={`/api/v1/online/artifacts/${artifact.sha256}`}>{content}</a>
            : <div className="artifact-row" key={artifact.object_id}>{content}</div>;
        })}
      </div>
      {mapping && mapping.decisions.length > 0 && <div className="mapping-summary">
        <div><Network /><div><strong>字段语义映射已生成</strong><p>AI 只读取研究目标和字段名，原始单元格从未交给模型，也没有被改写。</p></div></div>
        <div className="mapping-metrics"><span><strong>{mapping.mapped_count}</strong> 已映射</span><span><strong>{mapping.unmapped_count}</strong> 保留原名</span><span>{mapping.model_invocation ? "Qwen 辅助" : "确定性映射"}</span></div>
      </div>}
      {snapshot.online_structured_data?.datasets.map((dataset) => <StructuredPreview dataset={dataset} mappings={mapping?.decisions.filter((item) => item.dataset_id === dataset.dataset_id) ?? []} key={dataset.dataset_id} />)}
    </section>
  );
}

function StructuredPreview({ dataset, mappings }: { dataset: StructuredDatasetPreview; mappings: NonNullable<WorkbenchSnapshot["online_field_mapping"]>["decisions"] }) {
  const columns = dataset.columns.slice(0, dataset.preview_column_count);
  const values = new Map(dataset.cells.map((cell) => [`${cell.row_index}:${cell.column_index}`, cell.raw_value_json]));
  const mappingByColumn = new Map(mappings.map((item) => [item.column_index, item]));
  return (
    <article className="dataset-preview">
      <div className="dataset-heading">
        <div><span>{dataset.format.toUpperCase()}</span><h3>{dataset.row_count.toLocaleString()} 行 · {dataset.column_count} 列</h3><p>{dataset.parser_id} {dataset.parser_version}{dataset.truncated ? " · 有界预览" : " · 完整预览"}</p></div>
        <div className="dataset-actions"><a href={dataset.source_url} target="_blank" rel="noreferrer" title="打开来源"><ExternalLink /></a><a href={`/api/v1/online/artifacts/${dataset.artifact_sha256}`} title="下载原文件"><Download /></a></div>
      </div>
      <div className="table-wrap preview-table"><table><thead><tr><th>#</th>{columns.map((column) => {
        const fieldMapping = mappingByColumn.get(column.column_index);
        return <th key={column.column_index}>{column.name}<small>{column.non_empty_count} 有值 · {column.empty_count + column.null_count} 空</small>{fieldMapping && <span className={`mapping-chip ${fieldMapping.status}`} title={fieldMapping.rationale}>{fieldMapping.target_field ? `→ ${fieldMapping.target_field}` : "未确认语义"}{fieldMapping.status === "mapped" && ` · ${Math.round(fieldMapping.confidence * 100)}%`}</span>}</th>;
      })}</tr></thead><tbody>
        {Array.from({ length: dataset.preview_row_count }, (_, row) => <tr key={row}><td>{row + 1}</td>{columns.map((column) => <td className="mono" key={column.column_index}>{displayRawValue(values.get(`${row + 1}:${column.column_index}`) ?? "null")}</td>)}</tr>)}
      </tbody></table></div>
      <footer><code>{dataset.artifact_sha256}</code><span>数据集身份 {dataset.dataset_hash.slice(0, 16)}</span></footer>
    </article>
  );
}

function Evidence({ snapshot }: { snapshot: WorkbenchSnapshot }) {
  const [query, setQuery] = useState("");
  const rows = snapshot.evidence.filter((item) => `${item.field_name} ${item.raw_value} ${item.source_location}`.toLowerCase().includes(query.toLowerCase()));
  return (
    <section className="section-block">
      <div className="section-heading"><div><h2>证据与字段</h2><p>科学值只展示可回溯证据，不足项不会由 AI 编造</p></div><div className="search-box"><Search /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="筛选字段、值或位置" /></div></div>
      <div className="table-wrap"><table><thead><tr><th>字段</th><th>原始值</th><th>来源位置</th><th>提取方法</th><th>置信度</th><th>源哈希</th></tr></thead><tbody>
        {rows.map((item) => <tr key={item.evidence_id}><td><strong>{item.field_name}</strong></td><td className="mono">{snapshot.topic_data_status === "live_discovery" ? displayRawValue(item.raw_value) : item.raw_value}</td><td>{item.source_location}<small>{item.byte_range}</small></td><td>{item.method}</td><td>{Math.round(item.confidence * 100)}%</td><td><code>{item.source_hash.slice(0, 14)}</code></td></tr>)}
      </tbody></table></div>
    </section>
  );
}

function Delivery({ snapshot }: { snapshot: WorkbenchSnapshot }) {
  const download = async () => {
    if (snapshot.topic_data_status === "live_discovery") {
      if (snapshot.online_field_mapping?.decisions.length) window.location.assign("/api/v1/online/evidence-table.csv");
      return;
    }
    const response = await fetch(`/api/v1/demo/download-tickets/${encodeURIComponent(snapshot.package_filename)}`, { method: "POST" });
    if (response.ok) {
      const ticket = await response.json() as { download_url: string };
      window.location.assign(ticket.download_url);
    }
  };
  return (
    <>
      <section className="delivery-hero">
        <div><span className={snapshot.quality_gate_passed ? "pass" : "review"}>{snapshot.quality_gate_passed ? "质量门通过" : snapshot.topic_data_status === "live_discovery" ? "可审核数据表" : "需要复核"}</span><h2>{snapshot.formal_gold_available ? "正式数据与复现包已就绪" : snapshot.topic_data_status === "live_discovery" && snapshot.online_field_mapping?.decisions.length ? "多源证据长表已就绪" : "复核包已就绪，正式数据暂缓发布"}</h2><p>{snapshot.topic_data_status === "live_discovery" ? "原始值未改写 · 字段映射可复核 · 每行携带来源证据" : `${snapshot.evidence.length} 条证据 · ${snapshot.issues.length} 个问题 · ${snapshot.artifacts.length} 个原始文件`}</p></div>
        <button className="primary-action" disabled={snapshot.topic_data_status === "live_discovery" && !snapshot.online_field_mapping?.decisions.length} onClick={() => void download()}><Download />{snapshot.topic_data_status === "live_discovery" ? "下载多源证据 CSV" : "下载复现包"}</button>
      </section>
      <section className="section-block">
        <div className="section-heading"><div><h2>质量问题</h2><p>每个问题保留受影响字段、证据数量和明确动作</p></div><span>{snapshot.issues.length} 项</span></div>
        {snapshot.issues.length === 0 ? <div className="empty-state"><CheckCircle2 /><strong>当前没有阻塞问题</strong></div> : <div className="issue-list">{snapshot.issues.map((issue) => <article key={issue.issue_id}><CircleAlert /><div><strong>{issue.fields.join("、") || issue.code}</strong><p>{issue.detail}</p><span>{issue.severity} · {issue.evidence_count} 条证据 · {issue.action}</span></div></article>)}</div>}
      </section>
    </>
  );
}

function displayRawValue(value: string) {
  try {
    const parsed = JSON.parse(value) as unknown;
    if (parsed === null) return "null";
    if (typeof parsed === "string") return parsed;
    return String(parsed);
  } catch {
    return value;
  }
}

function Configuration() {
  const [configuration, setConfiguration] = useState<OnlineConfiguration | null>(null);
  const [dashscopeKey, setDashscopeKey] = useState("");
  const [serpapiKey, setSerpapiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("https://dashscope.aliyuncs.com/compatible-mode/v1");
  const [message, setMessage] = useState("");
  useEffect(() => { void api.configuration().then((value) => { setConfiguration(value); setBaseUrl(value.model_base_url ?? baseUrl); }); }, []);
  const save = async () => {
    setMessage("");
    try {
      const updated = await api.saveConfiguration({
        online_enabled: true,
        dashscope_api_key: dashscopeKey || null,
        serpapi_api_key: serpapiKey || null,
        qwen_base_url: baseUrl,
        bailian_region: configuration?.bailian_region ?? "cn-beijing",
        bailian_workspace_id: null,
        search_engine: configuration?.search_engine ?? "google",
        search_language: configuration?.search_language ?? "zh-cn",
        search_country: configuration?.search_country,
        query_planning_enabled: true,
        max_search_queries: configuration?.max_search_queries ?? 5,
        max_search_results: configuration?.max_search_results ?? 20,
        planner_model_id: configuration?.planner_model_id ?? "qwen-plus",
        assessment_model_id: configuration?.assessment_model_id ?? "qwen-turbo"
      });
      setConfiguration(updated); setDashscopeKey(""); setSerpapiKey(""); setMessage(updated.online_ready ? "联网能力已就绪" : "配置已保存，请补齐缺少的密钥");
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : "配置保存失败"); }
  };
  return (
    <section className="section-block settings-panel">
      <div className="section-heading"><div><h2>联网配置</h2><p>密钥仅写入本机忽略文件，页面不会回显原值</p></div><span className={configuration?.online_ready ? "subtle-badge" : "warning-badge"}>{configuration?.online_ready ? "已就绪" : "待配置"}</span></div>
      <div className="settings-form">
        <label><span><KeyRound />阿里云百炼 API Key</span><input type="password" value={dashscopeKey} onChange={(event) => setDashscopeKey(event.target.value)} placeholder={configuration?.credentials.find((item) => item.environment_variable === "DASHSCOPE_API_KEY")?.configured ? "已配置，留空保持原值" : "sk-..."} /></label>
        <label><span><KeyRound />SerpApi Key</span><input type="password" value={serpapiKey} onChange={(event) => setSerpapiKey(event.target.value)} placeholder={configuration?.credentials.find((item) => item.environment_variable === "SERPAPI_API_KEY")?.configured ? "已配置，留空保持原值" : "输入搜索 API Key"} /></label>
        <label className="wide"><span><Network />Qwen Base URL</span><input value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} /></label>
      </div>
      <div className="settings-actions"><span>{message}</span><button className="primary-action" onClick={() => void save()}><CheckCircle2 />保存并应用</button></div>
    </section>
  );
}

function Metric({ icon: Icon, value, label, detail }: { icon: typeof Activity; value: string | number; label: string; detail: string }) {
  return <div className="metric"><Icon /><div><strong>{value}</strong><span>{label}</span><small>{detail}</small></div></div>;
}

function QualityBadge({ snapshot }: { snapshot: WorkbenchSnapshot }) {
  return <span className={snapshot.quality_gate_passed ? "quality-pass" : "quality-review"}>{snapshot.quality_gate_passed ? <CheckCircle2 /> : <CircleAlert />}{snapshot.quality_gate_passed ? "质量门通过" : "需要复核"}</span>;
}

function ListColumn({ title, items = [] }: { title: string; items?: string[] }) {
  return <div><h3>{title}</h3><ul>{items.slice(0, 6).map((item) => <li key={item}>{item}</li>)}</ul></div>;
}

function LoadingState() {
  return <div className="loading-state"><RefreshCw className="spin" /><strong>正在载入研究工作台</strong><span>首次运行会构建可复现的参考数据链</span></div>;
}

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export default App;
