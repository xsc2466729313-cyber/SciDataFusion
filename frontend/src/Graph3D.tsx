import { useEffect, useMemo, useRef } from "react";
import ForceGraph3D from "react-force-graph-3d";
import type { ForceGraphMethods } from "react-force-graph-3d";
import type { GraphEdge, GraphNode } from "./types";

interface GraphProps {
  nodes: GraphNode[];
  edges: GraphEdge[];
  colors: Record<string, string>;
  onSelect: (node: GraphNode) => void;
}

export default function Graph3D({ nodes, edges, colors, onSelect }: GraphProps) {
  const graphRef = useRef<ForceGraphMethods | undefined>(undefined);
  const graph = useMemo(
    () => ({
      nodes: nodes.map((node) => ({ ...node, id: node.node_id })),
      links: edges.map((edge) => ({ ...edge }))
    }),
    [nodes, edges]
  );
  useEffect(() => {
    graphRef.current?.cameraPosition({ x: 0, y: 0, z: 150 });
  }, []);
  return (
    <div className="graph-canvas" aria-label="三维证据知识图谱">
      <ForceGraph3D
        ref={graphRef}
        graphData={graph}
        nodeLabel={(node) => `${String(node.label)} · ${String(node.kind)}`}
        nodeColor={(node) => colors[String(node.kind)] ?? "#64748b"}
        nodeVal={(node) => (node.kind === "evidence" ? 5 : 9)}
        nodeRelSize={7}
        linkSource="source"
        linkTarget="target"
        linkColor={() => "rgba(92,111,104,0.32)"}
        linkOpacity={0.68}
        linkWidth={1}
        linkDirectionalParticles={1}
        linkDirectionalParticleWidth={1.2}
        linkDirectionalParticleSpeed={0.002}
        backgroundColor="#f7faf9"
        showNavInfo={false}
        onNodeClick={(node) => onSelect(node as GraphNode)}
        width={900}
        height={510}
      />
    </div>
  );
}
