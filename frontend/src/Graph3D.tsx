import { useEffect, useMemo, useRef, useState } from "react";
import ForceGraph3D from "react-force-graph-3d";
import type { ForceGraphMethods, LinkObject, NodeObject } from "react-force-graph-3d";
import { Group, Object3D } from "three";
import SpriteText from "three-spritetext";
import type { GraphEdge, GraphNode } from "./types";

interface GraphProps {
  nodes: GraphNode[];
  edges: GraphEdge[];
  colors: Record<string, string>;
  kindLabels: Record<string, string>;
  relationLabels: Record<string, string>;
  onSelect: (node: GraphNode) => void;
  selectedNodeId?: string | null;
  highlightedNodeIds?: string[];
  persistentLabelKinds?: string[];
  radialLayout?: boolean;
}

interface RenderNode extends GraphNode {
  id: string;
  x?: number;
  y?: number;
  z?: number;
}

export default function Graph3D({
  nodes,
  edges,
  colors,
  kindLabels,
  relationLabels,
  onSelect,
  selectedNodeId = null,
  highlightedNodeIds = [],
  persistentLabelKinds = [],
  radialLayout = false
}: GraphProps) {
  const graphRef = useRef<ForceGraphMethods<NodeObject<RenderNode>, LinkObject<RenderNode, GraphEdge>> | undefined>(undefined);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const fittedRef = useRef(false);
  const [dimensions, setDimensions] = useState<{ width: number; height: number } | null>(null);
  const highlighted = useMemo(() => new Set(highlightedNodeIds), [highlightedNodeIds]);
  const persistentKinds = useMemo(() => new Set(persistentLabelKinds), [persistentLabelKinds]);
  const graph = useMemo(
    () => ({
      nodes: nodes.map((node) => ({ ...node, id: node.node_id })),
      links: edges.map((edge) => ({ ...edge }))
    }),
    [nodes, edges]
  );
  useEffect(() => { fittedRef.current = false; }, [graph]);
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const updateDimensions = () => {
      const rect = container.getBoundingClientRect();
      setDimensions({
        width: Math.max(1, Math.floor(rect.width)),
        height: Math.max(1, Math.floor(rect.height))
      });
    };
    updateDimensions();
    const observer = new ResizeObserver(updateDimensions);
    observer.observe(container);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    if (!selectedNodeId) return;
    const node = graph.nodes.find((item) => item.node_id === selectedNodeId) as RenderNode | undefined;
    if (!node || node.x === undefined || node.y === undefined || node.z === undefined) return;
    const length = Math.hypot(node.x, node.y, node.z) || 1;
    const distance = 160;
    const ratio = 1 + distance / length;
    graphRef.current?.cameraPosition(
      { x: node.x * ratio, y: node.y * ratio, z: node.z * ratio },
      { x: node.x, y: node.y, z: node.z },
      700
    );
  }, [graph.nodes, selectedNodeId]);

  const endpointId = (endpoint: unknown) => {
    if (typeof endpoint === "string") return endpoint;
    if (endpoint && typeof endpoint === "object" && "node_id" in endpoint) {
      return String((endpoint as { node_id: unknown }).node_id);
    }
    return "";
  };

  const selectedLink = (link: object) => {
    if (!selectedNodeId) return false;
    const value = link as { source: unknown; target: unknown };
    return endpointId(value.source) === selectedNodeId || endpointId(value.target) === selectedNodeId;
  };

  return (
    <div ref={containerRef} className="graph-canvas" aria-label="三维证据知识图谱">
      {dimensions && <ForceGraph3D
        ref={graphRef}
        graphData={graph}
        nodeLabel={(node) => `${String(node.label)} · ${kindLabels[String(node.kind)] ?? "其他"}`}
        nodeColor={(node) => {
          const id = String(node.node_id);
          if (id === selectedNodeId) return "#f4a261";
          if (highlighted.has(id)) return "#f0b44d";
          return colors[String(node.kind)] ?? "#64748b";
        }}
        nodeVal={(node) => {
          const id = String(node.node_id);
          if (id === selectedNodeId) return 18;
          if (highlighted.has(id)) return 14;
          if (node.kind === "disease") return 11;
          if (node.kind === "symptom") return 7;
          if (node.kind === "precaution") return 6;
          return node.kind === "evidence" ? 5 : 9;
        }}
        nodeRelSize={5.5}
        nodeResolution={20}
        nodeOpacity={0.94}
        nodeThreeObject={(node: RenderNode) => {
          const id = String(node.node_id);
          const showLabel = persistentKinds.has(String(node.kind)) || id === selectedNodeId || highlighted.has(id);
          if (!showLabel) return new Object3D();
          const label = new SpriteText(String(node.label));
          label.color = "#17352f";
          label.backgroundColor = id === selectedNodeId ? "rgba(255,244,220,0.96)" : "rgba(255,255,255,0.9)";
          label.textHeight = id === selectedNodeId ? 8 : 5.2;
          label.fontFace = '"Noto Sans SC", "Microsoft YaHei", sans-serif';
          label.fontWeight = "700";
          label.padding = [2.2, 1.4];
          label.borderRadius = 3;
          // Labels are children of the graph-managed node object. Keeping the
          // offset on the child prevents the force-graph position update from
          // resetting it on every simulation tick.
          label.position.set(0, id === selectedNodeId ? 17 : 13, 0);
          label.material.depthWrite = false;
          label.material.depthTest = false;
          label.renderOrder = 20;
          const labelGroup = new Group();
          labelGroup.add(label);
          return labelGroup;
        }}
        nodeThreeObjectExtend
        linkSource="source"
        linkTarget="target"
        linkColor={(link) => selectedLink(link) ? "rgba(220,103,59,0.9)" : "rgba(82,104,98,0.28)"}
        linkOpacity={0.74}
        linkWidth={(link) => selectedLink(link) ? 2.8 : 0.75}
        linkDirectionalArrowLength={(link) => selectedLink(link) ? 4 : 1.8}
        linkDirectionalArrowRelPos={0.82}
        linkDirectionalParticles={(link) => selectedLink(link) ? 3 : 0}
        linkDirectionalParticleWidth={2}
        linkDirectionalParticleSpeed={0.002}
        linkLabel={(link) => relationLabels[String(link.kind)] ?? "相关"}
        backgroundColor="#f5f8f7"
        showNavInfo={false}
        onNodeClick={(node) => onSelect(node as GraphNode)}
        onEngineStop={() => {
          if (fittedRef.current) return;
          fittedRef.current = true;
          graphRef.current?.zoomToFit(650, 54);
        }}
        dagMode={radialLayout ? "radialout" : undefined}
        dagLevelDistance={radialLayout ? 74 : undefined}
        warmupTicks={80}
        cooldownTicks={220}
        width={dimensions.width}
        height={dimensions.height}
      />}
    </div>
  );
}
