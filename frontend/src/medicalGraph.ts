import sourceGraph from "./data/disease-knowledge-graph.json";
import chineseLabels from "./data/disease-labels.zh.json";
import type { GraphEdge, GraphNode } from "./types";

interface SourceNode {
  id: string;
  type: "Disease" | "Symptom" | "Precaution";
  label: string;
}

interface SourceEdge {
  source: string;
  target: string;
  relation: "HAS_SYMPTOM" | "HAS_PRECAUTION";
}

interface SourceGraph {
  nodes: SourceNode[];
  edges: SourceEdge[];
}

export interface MedicalGraphNode extends GraphNode {
  original_label: string;
  degree: number;
}

const kindMap = {
  Disease: "disease",
  Symptom: "symptom",
  Precaution: "precaution"
} as const;

const relationMap = {
  HAS_SYMPTOM: "has_symptom",
  HAS_PRECAUTION: "has_precaution"
} as const;

const raw = sourceGraph as SourceGraph;
const labels = chineseLabels as Record<string, string>;
const degree = new Map<string, number>();
for (const edge of raw.edges) {
  degree.set(edge.source, (degree.get(edge.source) ?? 0) + 1);
  degree.set(edge.target, (degree.get(edge.target) ?? 0) + 1);
}

export const medicalGraphNodes: MedicalGraphNode[] = raw.nodes.map((node) => {
  const chineseLabel = labels[node.label];
  if (!chineseLabel) throw new Error(`医学图谱存在未翻译节点：${node.label}`);
  return {
    node_id: node.id,
    kind: kindMap[node.type],
    source_id: "疾病症状防护建议本地参考图谱",
    label: chineseLabel,
    original_label: node.label,
    trusted: false,
    degree: degree.get(node.id) ?? 0
  };
});

export const medicalGraphEdges: GraphEdge[] = raw.edges.map((edge) => ({
  source: edge.source,
  target: edge.target,
  kind: relationMap[edge.relation],
  evidence_refs: ["本地参考数据集关系"]
}));

export const medicalGraphCounts = {
  diseases: medicalGraphNodes.filter((node) => node.kind === "disease").length,
  symptoms: medicalGraphNodes.filter((node) => node.kind === "symptom").length,
  precautions: medicalGraphNodes.filter((node) => node.kind === "precaution").length,
  edges: medicalGraphEdges.length
};
