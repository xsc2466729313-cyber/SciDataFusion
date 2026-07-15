import * as THREE from "./three.module.min.js";

const KIND_META = Object.freeze({
  task: { label: "研究任务", color: "#e8f1ff", layer: 72 },
  evidence: { label: "证据", color: "#4eb9e6", layer: 28 },
  field: { label: "字段", color: "#48ce91", layer: -12 },
  quality_gate: { label: "质量门", color: "#f3ad55", layer: -52 },
  quality_issue: { label: "质量问题", color: "#f06f66", layer: -76 },
  memory: { label: "任务记忆", color: "#b9c8c2", layer: 54 },
});

const EDGE_LABELS = Object.freeze({
  contains: "包含",
  supports: "支持",
  violates: "违反",
  affects: "影响",
  derived_from: "派生自",
});

const FIELD_LABELS = Object.freeze({
  source_record_id: "来源记录 ID",
  observation_time: "观测时间",
  object_id: "天体 ID",
  magnitude: "星等",
  flux: "光通量",
  band: "波段",
});

const GATE_LABELS = Object.freeze({
  required_fields_complete: "必填字段完整",
  photometric_value_present: "测光值存在",
  required_field_provenance: "必填字段来源完整",
});

const escapeHtml = value => String(value ?? "").replace(
  /[&<>'"]/g,
  char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char],
);

function stableHash(value) {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

function displayNodeLabel(node) {
  if (node.kind === "task") return "科学数据整合任务";
  if (node.kind === "memory") return node.trusted ? "已批准任务记忆" : "隔离任务记忆";
  if (node.kind === "field") {
    const fieldName = node.label.replace(/^field\s+/, "");
    return `字段 · ${FIELD_LABELS[fieldName] || fieldName}`;
  }
  if (node.kind === "quality_gate") {
    const gateName = node.label.trim().split(/\s+/).at(-1);
    return `质量门 · ${GATE_LABELS[gateName] || gateName}`;
  }
  if (node.kind === "quality_issue") return "待审质量问题";
  if (node.kind === "evidence") {
    const evidence = node.label.match(/row\s+(\d+).*column\s+(\d+)/i);
    if (evidence) return `表格证据 · 第 ${evidence[1]} 行第 ${evidence[2]} 列`;
    return "字段级证据";
  }
  return node.label;
}

class EvidenceGraph3D {
  constructor(options) {
    this.canvas = options.canvas;
    this.tooltip = options.tooltip;
    this.inspector = options.inspector;
    this.status = options.status;
    this.count = options.count;
    this.root = options.root;
    this.nodes = [];
    this.edges = [];
    this.visibleEdges = [];
    this.nodeById = new Map();
    this.meshes = [];
    this.enabledKinds = new Set(Object.keys(KIND_META));
    this.selectedNode = null;
    this.hoveredNode = null;
    this.draggedNode = null;
    this.layoutPaused = false;
    this.layoutEnergy = 1;
    this.signature = "";
    this.pointer = new THREE.Vector2();
    this.raycaster = new THREE.Raycaster();
    this.dragPlane = new THREE.Plane();
    this.dragNormal = new THREE.Vector3();
    this.dragPoint = new THREE.Vector3();
    this.forceVector = new THREE.Vector3();
    this.projectedPoint = new THREE.Vector3();
    this.cameraTarget = new THREE.Vector3(0, 0, 0);
    this.cameraYaw = 0.62;
    this.cameraPitch = 0.18;
    this.cameraDistance = 360;
    this.pointerState = null;
    this.frameCount = 0;
    this.renderWidth = 0;
    this.renderHeight = 0;

    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color("#071410");
    this.scene.fog = new THREE.FogExp2("#071410", 0.0023);
    this.camera = new THREE.PerspectiveCamera(48, 1, 0.1, 1800);
    this.renderer = new THREE.WebGLRenderer({
      canvas: this.canvas,
      antialias: true,
      alpha: false,
      preserveDrawingBuffer: true,
      powerPreference: "high-performance",
    });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.08;

    this.graphGroup = new THREE.Group();
    this.scene.add(this.graphGroup);
    this.scene.add(new THREE.AmbientLight("#d9fff1", 1.9));
    const keyLight = new THREE.DirectionalLight("#a9d9ff", 4.2);
    keyLight.position.set(160, 210, 240);
    this.scene.add(keyLight);
    const rimLight = new THREE.PointLight("#51e0a1", 55, 680, 1.6);
    rimLight.position.set(-180, -80, 180);
    this.scene.add(rimLight);

    const grid = new THREE.GridHelper(620, 24, "#285347", "#15362d");
    grid.position.y = -118;
    grid.material.transparent = true;
    grid.material.opacity = 0.42;
    this.scene.add(grid);

    this.edgeGeometry = new THREE.BufferGeometry();
    this.edgeMaterial = new THREE.LineBasicMaterial({
      vertexColors: true,
      transparent: true,
      opacity: 0.62,
    });
    this.edgeLines = new THREE.LineSegments(this.edgeGeometry, this.edgeMaterial);
    this.graphGroup.add(this.edgeLines);

    this._bindControls();
    this._bindPointerEvents();
    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(this.canvas);
    this._updateCamera();
    this.resize();
    this.renderer.setAnimationLoop(() => this._frame());
  }

  setData(nodes, edges) {
    const nextSignature = `${nodes.map(node => node.node_id).join("|")}::${edges.length}`;
    if (nextSignature === this.signature) {
      this.resize();
      return;
    }
    this.signature = nextSignature;
    this._clearGraph();
    this.nodes = nodes.map((node, index) => ({
      ...node,
      index,
      velocity: new THREE.Vector3(),
      pinned: false,
      connections: [],
    }));
    this.nodeById = new Map(this.nodes.map(node => [node.node_id, node]));
    this.edges = edges
      .map((edge, index) => ({
        ...edge,
        index,
        sourceNode: this.nodeById.get(edge.source),
        targetNode: this.nodeById.get(edge.target),
      }))
      .filter(edge => edge.sourceNode && edge.targetNode);
    this.edges.forEach(edge => {
      edge.sourceNode.connections.push({ edge, neighbor: edge.targetNode, direction: "out" });
      edge.targetNode.connections.push({ edge, neighbor: edge.sourceNode, direction: "in" });
    });
    this.nodes.forEach((node, index) => this._createNode(node, index));
    this._refreshVisibility();
    this.layoutEnergy = 1;
    const firstNode = this.nodes.find(
      node => node.kind === "field" && node.label.endsWith("magnitude"),
    ) || this.nodes.find(node => node.kind === "field") || this.nodes[0] || null;
    this._selectNode(firstNode);
    this.canvas.dataset.graphReady = "true";
    this.canvas.dataset.nodeCount = String(this.nodes.length);
    this.canvas.dataset.edgeCount = String(this.edges.length);
    this._updateStatus();
  }

  resize() {
    const rect = this.canvas.getBoundingClientRect();
    const width = Math.max(1, Math.round(rect.width));
    const height = Math.max(1, Math.round(rect.height));
    if (this.renderWidth !== width || this.renderHeight !== height) {
      this.renderWidth = width;
      this.renderHeight = height;
      this.renderer.setSize(width, height, false);
      this.camera.aspect = width / height;
      this.camera.updateProjectionMatrix();
    }
  }

  _clearGraph() {
    this.meshes.forEach(mesh => {
      this.graphGroup.remove(mesh);
      mesh.geometry.dispose();
      mesh.material.dispose();
    });
    this.meshes = [];
    this.edgeGeometry.setAttribute("position", new THREE.BufferAttribute(new Float32Array(), 3));
    this.edgeGeometry.setAttribute("color", new THREE.BufferAttribute(new Float32Array(), 3));
  }

  _createNode(node, index) {
    const meta = KIND_META[node.kind] || KIND_META.memory;
    const seed = stableHash(node.node_id);
    const angle = index * 2.399963229728653 + (seed % 1000) / 1000;
    const radius = 72 + Math.sqrt(index + 1) * 10 + (seed % 23);
    const y = meta.layer + ((seed >> 8) % 31) - 15;
    const geometry = new THREE.SphereGeometry(node.trusted ? 5.2 : 4.1, 18, 14);
    const material = new THREE.MeshStandardMaterial({
      color: meta.color,
      emissive: meta.color,
      emissiveIntensity: node.trusted ? 0.22 : 0.08,
      metalness: 0.18,
      roughness: 0.38,
      transparent: true,
      opacity: node.trusted ? 1 : 0.58,
    });
    const mesh = new THREE.Mesh(geometry, material);
    mesh.position.set(Math.cos(angle) * radius, y, Math.sin(angle) * radius);
    mesh.userData.node = node;
    node.mesh = mesh;
    node.baseColor = new THREE.Color(meta.color);
    this.meshes.push(mesh);
    this.graphGroup.add(mesh);
  }

  _bindControls() {
    this.root.querySelectorAll(".graph-filter[data-kind]").forEach(button => {
      button.addEventListener("click", () => {
        const kind = button.dataset.kind;
        if (this.enabledKinds.has(kind)) {
          if (this.enabledKinds.size === 1) return;
          this.enabledKinds.delete(kind);
          button.setAttribute("aria-pressed", "false");
        } else {
          this.enabledKinds.add(kind);
          button.setAttribute("aria-pressed", "true");
        }
        this._refreshVisibility();
        this.layoutEnergy = 0.6;
        if (this.selectedNode && !this.selectedNode.mesh.visible) {
          this._selectNode(this.nodes.find(node => node.mesh.visible) || null);
        }
      });
    });
    this.root.querySelector("#graph-reset").addEventListener("click", () => {
      this.cameraYaw = 0.62;
      this.cameraPitch = 0.18;
      this.cameraDistance = 360;
      this.cameraTarget.set(0, 0, 0);
      this.nodes.forEach(node => { node.pinned = false; });
      this.layoutEnergy = 0.8;
      this._updateCamera();
    });
    this.root.querySelector("#graph-pause").addEventListener("click", event => {
      this.layoutPaused = !this.layoutPaused;
      event.currentTarget.textContent = this.layoutPaused ? "继续布局" : "暂停布局";
      event.currentTarget.setAttribute("aria-pressed", String(this.layoutPaused));
      this._updateStatus();
    });
  }

  _bindPointerEvents() {
    this.canvas.addEventListener("pointerdown", event => this._pointerDown(event));
    this.canvas.addEventListener("pointermove", event => this._pointerMove(event));
    this.canvas.addEventListener("pointerup", event => this._pointerUp(event));
    this.canvas.addEventListener("pointercancel", event => this._pointerUp(event));
    this.canvas.addEventListener("pointerleave", () => {
      if (!this.pointerState) this._setHoveredNode(null);
    });
    this.canvas.addEventListener("wheel", event => {
      event.preventDefault();
      this.cameraDistance = THREE.MathUtils.clamp(
        this.cameraDistance * Math.exp(event.deltaY * 0.0012),
        95,
        760,
      );
      this._updateCamera();
    }, { passive: false });
    this.canvas.addEventListener("dblclick", event => {
      const node = this._pickNode(event);
      if (node) {
        node.pinned = !node.pinned;
        this._selectNode(node);
        this._updateStatus();
      }
    });
    this.canvas.addEventListener("keydown", event => {
      if (event.key.toLowerCase() === "r") this.root.querySelector("#graph-reset").click();
      if (event.key === " ") {
        event.preventDefault();
        this.root.querySelector("#graph-pause").click();
      }
    });
  }

  _pointerDown(event) {
    const node = this._pickNode(event);
    this.pointerState = {
      pointerId: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      lastX: event.clientX,
      lastY: event.clientY,
      moved: false,
      orbiting: !node,
    };
    this.canvas.setPointerCapture(event.pointerId);
    if (node) {
      this.draggedNode = node;
      node.pinned = true;
      this.camera.getWorldDirection(this.dragNormal);
      this.dragPlane.setFromNormalAndCoplanarPoint(this.dragNormal, node.mesh.position);
      this._selectNode(node);
      this._updateStatus();
    }
  }

  _pointerMove(event) {
    if (!this.pointerState) {
      this._setHoveredNode(this._pickNode(event), event);
      return;
    }
    const dx = event.clientX - this.pointerState.lastX;
    const dy = event.clientY - this.pointerState.lastY;
    this.pointerState.lastX = event.clientX;
    this.pointerState.lastY = event.clientY;
    if (Math.hypot(event.clientX - this.pointerState.x, event.clientY - this.pointerState.y) > 3) {
      this.pointerState.moved = true;
    }
    if (this.draggedNode) {
      this._setPointer(event);
      this.raycaster.setFromCamera(this.pointer, this.camera);
      if (this.raycaster.ray.intersectPlane(this.dragPlane, this.dragPoint)) {
        this.draggedNode.mesh.position.copy(this.dragPoint);
        this.draggedNode.velocity.set(0, 0, 0);
        this.layoutEnergy = Math.max(this.layoutEnergy, 0.25);
        this._writeEdges();
      }
      return;
    }
    if (this.pointerState.orbiting) {
      this.cameraYaw -= dx * 0.006;
      this.cameraPitch = THREE.MathUtils.clamp(this.cameraPitch + dy * 0.005, -1.15, 1.15);
      this._updateCamera();
    }
  }

  _pointerUp(event) {
    if (!this.pointerState) return;
    if (!this.pointerState.moved && !this.draggedNode) {
      const node = this._pickNode(event);
      if (node) this._selectNode(node);
    }
    if (this.canvas.hasPointerCapture(event.pointerId)) {
      this.canvas.releasePointerCapture(event.pointerId);
    }
    this.draggedNode = null;
    this.pointerState = null;
    this._updateStatus();
  }

  _setPointer(event) {
    const rect = this.canvas.getBoundingClientRect();
    this.pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
    this.pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
  }

  _pickNode(event) {
    this._setPointer(event);
    this.raycaster.setFromCamera(this.pointer, this.camera);
    const hits = this.raycaster.intersectObjects(this.meshes.filter(mesh => mesh.visible), false);
    return hits.length ? hits[0].object.userData.node : null;
  }

  _setHoveredNode(node, event = null) {
    if (node === this.hoveredNode) {
      if (node && event) this._positionTooltip(event);
      return;
    }
    const previous = this.hoveredNode;
    this.hoveredNode = node;
    if (previous && previous !== this.selectedNode) previous.mesh.scale.setScalar(1);
    if (node && node !== this.selectedNode) node.mesh.scale.setScalar(1.28);
    if (!node) {
      this.tooltip.hidden = true;
      return;
    }
    const meta = KIND_META[node.kind] || KIND_META.memory;
    this.tooltip.innerHTML = `<strong>${escapeHtml(displayNodeLabel(node))}</strong><span>${escapeHtml(meta.label)} · ${node.connections.length} 条关系</span>`;
    this.tooltip.hidden = false;
    if (event) this._positionTooltip(event);
  }

  _positionTooltip(event) {
    const rootRect = this.root.querySelector(".graph-viewport").getBoundingClientRect();
    const left = THREE.MathUtils.clamp(event.clientX - rootRect.left + 14, 8, rootRect.width - 230);
    const top = THREE.MathUtils.clamp(event.clientY - rootRect.top + 14, 8, rootRect.height - 76);
    this.tooltip.style.transform = `translate(${left}px, ${top}px)`;
  }

  _selectNode(node) {
    this.selectedNode = node;
    this.canvas.dataset.selectedNode = node ? node.node_id : "";
    this.nodes.forEach(item => {
      const selected = item === node;
      item.mesh.scale.setScalar(selected ? 1.62 : item === this.hoveredNode ? 1.28 : 1);
      item.mesh.material.emissiveIntensity = selected ? 1.15 : item.trusted ? 0.22 : 0.08;
      item.mesh.material.opacity = node && item !== node && !item.connections.some(link => link.neighbor === node)
        ? 0.28
        : item.trusted ? 1 : 0.58;
    });
    this._writeEdges();
    this._renderInspector(node);
  }

  _renderInspector(node) {
    if (!node) {
      this.inspector.innerHTML = '<div class="graph-empty"><strong>未选择节点</strong><span>图谱已就绪</span></div>';
      return;
    }
    const meta = KIND_META[node.kind] || KIND_META.memory;
    const relations = node.connections.slice(0, 10).map(connection => {
      const direction = connection.direction === "out" ? "→" : "←";
      const refs = connection.edge.evidence_refs?.length || 0;
      return `<li><span class="graph-relation-mark">${direction}</span><div><strong>${escapeHtml(displayNodeLabel(connection.neighbor))}</strong><span>${escapeHtml(EDGE_LABELS[connection.edge.kind] || connection.edge.kind)} · ${refs} 个证据引用</span></div></li>`;
    }).join("");
    this.inspector.innerHTML = `
      <div class="graph-node-heading">
        <i style="--node-color:${escapeHtml(meta.color)}"></i>
        <div><span>${escapeHtml(meta.label)}</span><h3>${escapeHtml(displayNodeLabel(node))}</h3></div>
      </div>
      <dl class="graph-node-facts">
        <div><dt>可信状态</dt><dd>${node.trusted ? "已验证事实" : "隔离 / 待审"}</dd></div>
        <div><dt>连接数量</dt><dd>${node.connections.length}</dd></div>
        <div class="wide"><dt>来源 ID</dt><dd class="graph-code">${escapeHtml(node.source_id)}</dd></div>
        <div class="wide"><dt>节点 ID</dt><dd class="graph-code">${escapeHtml(node.node_id)}</dd></div>
      </dl>
      <div class="graph-relations"><h4>直接关系</h4><ul>${relations || "<li>没有直接关系</li>"}</ul></div>
    `;
  }

  _refreshVisibility() {
    this.nodes.forEach(node => {
      node.mesh.visible = this.enabledKinds.has(node.kind);
    });
    this.visibleEdges = this.edges.filter(
      edge => edge.sourceNode.mesh.visible && edge.targetNode.mesh.visible,
    );
    this.edgeGeometry.setAttribute(
      "position",
      new THREE.BufferAttribute(new Float32Array(this.visibleEdges.length * 6), 3),
    );
    this.edgeGeometry.setAttribute(
      "color",
      new THREE.BufferAttribute(new Float32Array(this.visibleEdges.length * 6), 3),
    );
    this._writeEdges();
    const visibleNodes = this.nodes.filter(node => node.mesh.visible).length;
    this.canvas.dataset.activeNodes = String(visibleNodes);
    this.canvas.dataset.activeEdges = String(this.visibleEdges.length);
    this.count.textContent = `${visibleNodes} 个节点 · ${this.visibleEdges.length} 条关系`;
    this._updateStatus();
  }

  _writeEdges() {
    const positions = this.edgeGeometry.getAttribute("position");
    const colors = this.edgeGeometry.getAttribute("color");
    if (!positions || !colors) return;
    const neutral = new THREE.Color("#34584e");
    const dimmed = new THREE.Color("#18372f");
    const active = new THREE.Color("#8ff0c5");
    this.visibleEdges.forEach((edge, index) => {
      const offset = index * 2;
      positions.setXYZ(offset, edge.sourceNode.mesh.position.x, edge.sourceNode.mesh.position.y, edge.sourceNode.mesh.position.z);
      positions.setXYZ(offset + 1, edge.targetNode.mesh.position.x, edge.targetNode.mesh.position.y, edge.targetNode.mesh.position.z);
      const related = this.selectedNode && (edge.sourceNode === this.selectedNode || edge.targetNode === this.selectedNode);
      const color = this.selectedNode ? related ? active : dimmed : neutral;
      colors.setXYZ(offset, color.r, color.g, color.b);
      colors.setXYZ(offset + 1, color.r, color.g, color.b);
    });
    positions.needsUpdate = true;
    colors.needsUpdate = true;
    this.edgeGeometry.computeBoundingSphere();
  }

  _simulate() {
    const activeNodes = this.nodes.filter(node => node.mesh.visible);
    if (this.layoutPaused || this.layoutEnergy < 0.002 || this.draggedNode) return;
    for (let left = 0; left < activeNodes.length; left += 1) {
      for (let right = left + 1; right < activeNodes.length; right += 1) {
        const a = activeNodes[left];
        const b = activeNodes[right];
        const delta = this.forceVector.subVectors(a.mesh.position, b.mesh.position);
        const distanceSquared = Math.max(80, delta.lengthSq());
        const force = (1250 * this.layoutEnergy) / distanceSquared;
        delta.normalize().multiplyScalar(force);
        if (!a.pinned) a.velocity.add(delta);
        if (!b.pinned) b.velocity.sub(delta);
      }
    }
    this.visibleEdges.forEach(edge => {
      const delta = this.forceVector.subVectors(
        edge.targetNode.mesh.position,
        edge.sourceNode.mesh.position,
      );
      const distance = Math.max(0.001, delta.length());
      const target = edge.kind === "contains" ? 50 : 66;
      const force = (distance - target) * 0.0028 * this.layoutEnergy;
      delta.normalize().multiplyScalar(force);
      if (!edge.sourceNode.pinned) edge.sourceNode.velocity.add(delta);
      if (!edge.targetNode.pinned) edge.targetNode.velocity.sub(delta);
    });
    activeNodes.forEach(node => {
      if (node.pinned) return;
      const meta = KIND_META[node.kind] || KIND_META.memory;
      node.velocity.x += -node.mesh.position.x * 0.00035 * this.layoutEnergy;
      node.velocity.z += -node.mesh.position.z * 0.00035 * this.layoutEnergy;
      node.velocity.y += (meta.layer - node.mesh.position.y) * 0.0012 * this.layoutEnergy;
      node.velocity.multiplyScalar(0.86);
      node.mesh.position.add(node.velocity);
    });
    this.layoutEnergy *= 0.993;
    this._writeEdges();
  }

  _updateCamera() {
    const horizontal = Math.cos(this.cameraPitch) * this.cameraDistance;
    this.camera.position.set(
      this.cameraTarget.x + Math.sin(this.cameraYaw) * horizontal,
      this.cameraTarget.y + Math.sin(this.cameraPitch) * this.cameraDistance,
      this.cameraTarget.z + Math.cos(this.cameraYaw) * horizontal,
    );
    this.camera.lookAt(this.cameraTarget);
  }

  _updateStatus() {
    if (!this.status) return;
    const pinned = this.nodes.filter(node => node.pinned).length;
    const state = this.layoutPaused ? "布局已暂停" : "3D 布局运行中";
    this.status.textContent = pinned ? `${state} · ${pinned} 个节点已固定` : state;
  }

  _frame() {
    if (this.root.offsetParent === null) return;
    this.resize();
    this._simulate();
    this.renderer.render(this.scene, this.camera);
    this.frameCount += 1;
    if (this.frameCount % 10 === 0) {
      this.canvas.dataset.renderedFrames = String(this.frameCount);
      if (this.selectedNode) {
        this.projectedPoint.copy(this.selectedNode.mesh.position).project(this.camera);
        this.canvas.dataset.selectedX = String((this.projectedPoint.x + 1) * this.renderWidth / 2);
        this.canvas.dataset.selectedY = String((-this.projectedPoint.y + 1) * this.renderHeight / 2);
      }
    }
  }
}

export function createEvidenceGraph(options) {
  try {
    return new EvidenceGraph3D(options);
  } catch (error) {
    options.canvas.dataset.graphReady = "false";
    options.inspector.innerHTML = `<div class="graph-empty"><strong>3D 图谱不可用</strong><span>${escapeHtml(error.message)}</span></div>`;
    options.status.textContent = "当前浏览器未能初始化 WebGL";
    return { setData() {}, resize() {} };
  }
}
