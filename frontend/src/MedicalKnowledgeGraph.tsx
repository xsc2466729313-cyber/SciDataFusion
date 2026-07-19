import { Search, ShieldAlert, Target, X } from "lucide-react";
import { useMemo, useState } from "react";
import Graph3D from "./Graph3D";
import {
  medicalGraphCounts,
  medicalGraphEdges,
  medicalGraphNodes,
  type MedicalGraphNode
} from "./medicalGraph";
import type { GraphEdge } from "./types";

const colors: Record<string, string> = {
  disease: "#c24d3d",
  symptom: "#2478a8",
  precaution: "#23815f"
};

const kindLabels: Record<string, string> = {
  disease: "疾病",
  symptom: "症状或病史",
  precaution: "防护建议"
};

const relationLabels: Record<string, string> = {
  has_symptom: "可能伴随症状",
  has_precaution: "数据集建议措施"
};

export default function MedicalKnowledgeGraph() {
  const [query, setQuery] = useState("");
  const [selectedNode, setSelectedNode] = useState<MedicalGraphNode | null>(null);
  const normalizedQuery = query.trim().toLocaleLowerCase("zh-CN");
  const nodeById = useMemo(
    () => new Map(medicalGraphNodes.map((node) => [node.node_id, node])),
    []
  );
  const matchingNodes = useMemo(
    () => normalizedQuery
      ? medicalGraphNodes.filter((node) =>
          `${node.label} ${node.original_label}`.toLocaleLowerCase("zh-CN").includes(normalizedQuery)
        )
      : [],
    [normalizedQuery]
  );
  const visibleGraph = useMemo(() => {
    if (!normalizedQuery) return { nodes: medicalGraphNodes, edges: medicalGraphEdges };
    const matches = new Set(matchingNodes.map((node) => node.node_id));
    const visible = new Set(matches);
    for (const edge of medicalGraphEdges) {
      if (matches.has(edge.source) || matches.has(edge.target)) {
        visible.add(edge.source);
        visible.add(edge.target);
      }
    }
    return {
      nodes: medicalGraphNodes.filter((node) => visible.has(node.node_id)),
      edges: medicalGraphEdges.filter((edge) => visible.has(edge.source) && visible.has(edge.target))
    };
  }, [matchingNodes, normalizedQuery]);
  const selectedRelations = useMemo(
    () => selectedNode
      ? medicalGraphEdges.filter(
          (edge) => edge.source === selectedNode.node_id || edge.target === selectedNode.node_id
        )
      : [],
    [selectedNode]
  );

  const selectNode = (node: MedicalGraphNode) => {
    setSelectedNode(node);
  };

  return (
    <div className="medical-page">
      <header className="medical-titlebar">
        <div>
          <span>本地知识图谱 · 全中文呈现</span>
          <h2>疾病、症状与防护建议三维图谱</h2>
          <p>从参考项目导入完整关系数据，按疾病向症状与建议措施展开。</p>
        </div>
        <div className="medical-disclaimer"><ShieldAlert /><span>仅用于数据关系浏览，不构成诊断或治疗建议</span></div>
      </header>

      <div className="medical-metrics" aria-label="医学图谱统计">
        <GraphMetric value={medicalGraphCounts.diseases} label="疾病" color={colors.disease} />
        <GraphMetric value={medicalGraphCounts.symptoms} label="症状或病史" color={colors.symptom} />
        <GraphMetric value={medicalGraphCounts.precautions} label="防护建议" color={colors.precaution} />
        <GraphMetric value={medicalGraphCounts.edges} label="关系" color="#675f56" />
      </div>

      <section className="medical-graph-workspace">
        <div className="medical-graph-main">
          <div className="medical-toolbar">
            <label className="medical-search">
              <Search />
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="搜索疾病、症状或建议，例如：糖尿病、头痛"
              />
              {query && <button type="button" onClick={() => setQuery("")} title="清空搜索"><X /></button>}
            </label>
            <span>{visibleGraph.nodes.length} 个节点 · {visibleGraph.edges.length} 条关系</span>
          </div>
          <Graph3D
            nodes={visibleGraph.nodes}
            edges={visibleGraph.edges}
            onSelect={(node) => selectNode(node as MedicalGraphNode)}
            colors={colors}
            kindLabels={kindLabels}
            relationLabels={relationLabels}
            selectedNodeId={selectedNode?.node_id ?? null}
            highlightedNodeIds={matchingNodes.map((node) => node.node_id)}
            persistentLabelKinds={normalizedQuery ? ["disease", "symptom", "precaution"] : ["disease"]}
            radialLayout
          />
          <div className="graph-legend medical-legend">
            {Object.entries(colors).map(([kind, color]) => (
              <span key={kind}><i style={{ backgroundColor: color }} />{kindLabels[kind]}</span>
            ))}
            <small>疾病名称持续显示；搜索或选中后突出相邻关系</small>
          </div>
        </div>

        <aside className="medical-inspector">
          {normalizedQuery && matchingNodes.length > 0 && (
            <div className="medical-results">
              <h3>搜索结果</h3>
              {matchingNodes.slice(0, 8).map((node) => (
                <button key={node.node_id} onClick={() => selectNode(node)}>
                  <i style={{ backgroundColor: colors[node.kind] }} />
                  <span><strong>{node.label}</strong><small>{kindLabels[node.kind]}</small></span>
                </button>
              ))}
            </div>
          )}
          {normalizedQuery && matchingNodes.length === 0 && <div className="medical-no-result">没有匹配的中文或英文名称</div>}
          {selectedNode ? (
            <NodeDetails
              node={selectedNode}
              relations={selectedRelations}
              nodeById={nodeById}
              onSelect={selectNode}
            />
          ) : (
            <div className="empty-inspector medical-empty"><Target /><p>搜索或点击节点，查看中文名称及其直接关系</p></div>
          )}
        </aside>
      </section>
    </div>
  );
}

function GraphMetric({ value, label, color }: { value: number; label: string; color: string }) {
  return <div><i style={{ backgroundColor: color }} /><strong>{value}</strong><span>{label}</span></div>;
}

function NodeDetails({
  node,
  relations,
  nodeById,
  onSelect
}: {
  node: MedicalGraphNode;
  relations: GraphEdge[];
  nodeById: Map<string, MedicalGraphNode>;
  onSelect: (node: MedicalGraphNode) => void;
}) {
  return (
    <div className="medical-node-details">
      <span className="medical-node-kind" style={{ color: colors[node.kind] }}>{kindLabels[node.kind]}</span>
      <h3>{node.label}</h3>
      <dl>
        <dt>直接关系</dt><dd>{relations.length} 条</dd>
        <dt>数据来源</dt><dd>{node.source_id}</dd>
      </dl>
      <div className="medical-relation-list">
        <h4>直接关联</h4>
        {relations.slice(0, 24).map((edge, index) => {
          const outgoing = edge.source === node.node_id;
          const other = nodeById.get(outgoing ? edge.target : edge.source);
          if (!other) return null;
          return (
            <button key={`${edge.source}-${edge.target}-${index}`} onClick={() => onSelect(other)}>
              <i style={{ backgroundColor: colors[other.kind] }} />
              <span><strong>{other.label}</strong><small>{relationLabels[edge.kind]} · {kindLabels[other.kind]}</small></span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
