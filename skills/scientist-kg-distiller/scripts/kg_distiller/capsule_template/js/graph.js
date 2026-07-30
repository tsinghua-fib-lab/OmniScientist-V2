(function () {
  const ns = "http://www.w3.org/2000/svg";

  function svgElement(name, attrs) {
    const node = document.createElementNS(ns, name);
    Object.entries(attrs || {}).forEach(([key, value]) => node.setAttribute(key, value));
    return node;
  }

  function splitLabel(value, size) {
    const label = String(value || "");
    const lines = [];
    for (let index = 0; index < label.length; index += size) {
      lines.push(label.slice(index, index + size));
    }
    return lines.slice(0, 2);
  }

  window.createPersonalityGraph = function (container, model, onSelect) {
    const compact = window.matchMedia("(max-width: 720px)").matches;
    const width = compact ? 720 : 900;
    const height = compact ? 820 : 760;
    const center = { x: width / 2, y: compact ? 410 : 385 };
    const positions = {};
    const nodeData = new Map();
    const edgeData = [];
    const evidenceEdges = [];

    const corePositions = compact
      ? [
        { x: 360, y: 230 },
        { x: 220, y: 440 },
        { x: 500, y: 440 }
      ]
      : [
        { x: 450, y: 230 },
        { x: 300, y: 465 },
        { x: 600, y: 465 }
      ];
    const patternRadius = compact
      ? { x: 295, y: 335 }
      : { x: 355, y: 315 };
    const patternPositions = model.patterns.map((_, index) => {
      const angle = -Math.PI / 2 + index * Math.PI * 2 / model.patterns.length;
      return {
        x: center.x + Math.cos(angle) * patternRadius.x,
        y: center.y + Math.sin(angle) * patternRadius.y
      };
    });

    model.cores.forEach((node, index) => {
      positions[node.id] = corePositions[index];
      nodeData.set(node.id, { kind: "core", data: node });
    });
    positions[model.tone.id] = center;
    nodeData.set(model.tone.id, { kind: "tone", data: model.tone });
    model.patterns.forEach((node, index) => {
      positions[node.id] = patternPositions[index];
      nodeData.set(node.id, { kind: "pattern", data: node });
    });

    const edgeKeys = new Set();
    function rememberEdge(edge) {
      const key = `${edge.from}|${edge.to}|${edge.type}`;
      if (!positions[edge.from] || !positions[edge.to] || edgeKeys.has(key)) return;
      edgeKeys.add(key);
      edgeData.push(edge);
    }

    model.cores.forEach((node) => {
      (node.summarized_patterns || []).forEach((patternId) => {
        rememberEdge({ from: node.id, to: patternId, type: "summary" });
      });
    });
    if (!edgeData.length) {
      (model.relations.summarizes || []).forEach((edge) => {
        rememberEdge({ ...edge, type: "summary" });
      });
    }
    ["enables", "reinforces"].forEach((type) => {
      (model.relations[type] || []).forEach((edge) => {
        rememberEdge({ ...edge, type });
      });
    });
    (model.relations.tension || []).forEach((edge) => {
      const between = edge.between || [];
      rememberEdge({
        from: between[0],
        to: between[1],
        type: "tension",
        context: edge.context,
        reason: edge.reason
      });
    });

    const svg = svgElement("svg", {
      viewBox: `0 0 ${width} ${height}`,
      role: "img",
      "aria-label": "三个人格核心、一个语气原句节点、七种思维模式和原始论文证据组成的知识图谱",
      preserveAspectRatio: "xMidYMid meet"
    });
    const defs = buildDefinitions();
    const backdropLayer = svgElement("g", { "aria-hidden": "true" });
    const edgeLayer = svgElement("g");
    const nodeLayer = svgElement("g");
    const evidenceLayer = svgElement("g");
    svg.append(defs, backdropLayer, edgeLayer, nodeLayer, evidenceLayer);

    drawBackdrop();
    edgeData.forEach(drawEdge);
    model.cores.forEach((node) => drawNode(node, "core", 69));
    drawNode(model.tone, "tone", 55);
    model.patterns.forEach((node) => drawNode(node, "pattern", 51));
    container.replaceChildren(svg);

    function buildDefinitions() {
      const definitions = svgElement("defs");
      const pattern = radialGradient(
        "pattern-gradient",
        ["#24365d", "#0a0e24"],
        [0.72, 0.98]
      );
      const violet = radialGradient(
        "core-violet",
        ["#7458db", "#181139"],
        [0.72, 0.98]
      );
      const blue = radialGradient(
        "core-blue",
        ["#376fce", "#0d1839"],
        [0.72, 0.98]
      );
      const cyan = radialGradient(
        "core-cyan",
        ["#188cb9", "#081f36"],
        [0.78, 0.98]
      );
      const tone = radialGradient(
        "tone-gradient",
        ["#9b6dff", "#10233e"],
        [0.8, 0.99]
      );
      const coreGlow = svgElement("filter", {
        id: "core-glow", x: "-70%", y: "-70%", width: "240%", height: "240%"
      });
      coreGlow.appendChild(svgElement("feDropShadow", {
        dx: "0", dy: "0", stdDeviation: "8",
        "flood-color": "#7f83ff", "flood-opacity": "0.45"
      }));
      const selectedGlow = svgElement("filter", {
        id: "selected-glow", x: "-80%", y: "-80%", width: "260%", height: "260%"
      });
      selectedGlow.appendChild(svgElement("feDropShadow", {
        dx: "0", dy: "0", stdDeviation: "8",
        "flood-color": "#55d9ff", "flood-opacity": "0.72"
      }));
      const edgeGlow = svgElement("filter", {
        id: "edge-glow", x: "-40%", y: "-200%", width: "180%", height: "500%"
      });
      edgeGlow.appendChild(svgElement("feDropShadow", {
        dx: "0", dy: "0", stdDeviation: "4",
        "flood-color": "#9bdfff", "flood-opacity": "0.9"
      }));
      const arrow = svgElement("marker", {
        id: "arrow-enable",
        markerWidth: "7",
        markerHeight: "7",
        refX: "6",
        refY: "3.5",
        orient: "auto",
        markerUnits: "strokeWidth"
      });
      arrow.appendChild(svgElement("path", {
        d: "M0,0 L7,3.5 L0,7 Z",
        class: "arrow-enable"
      }));
      definitions.append(
        pattern, violet, blue, cyan, tone, coreGlow, selectedGlow, edgeGlow, arrow
      );
      return definitions;
    }

    function radialGradient(id, colors, opacities) {
      const gradient = svgElement("radialGradient", {
        id, cx: "38%", cy: "28%", r: "76%"
      });
      gradient.append(
        svgElement("stop", {
          offset: "0%",
          "stop-color": colors[0],
          "stop-opacity": opacities[0]
        }),
        svgElement("stop", {
          offset: "100%",
          "stop-color": colors[1],
          "stop-opacity": opacities[1]
        })
      );
      return gradient;
    }

    function drawBackdrop() {
      [
        [center.x, center.y, compact ? 255 : 270, compact ? 315 : 325],
        [center.x, center.y, compact ? 315 : 365, compact ? 370 : 345],
        [center.x, center.y, compact ? 190 : 210, compact ? 225 : 235]
      ].forEach(([cx, cy, rx, ry]) => {
        backdropLayer.appendChild(svgElement("ellipse", {
          cx, cy, rx, ry, class: "kg-orbit"
        }));
      });
      for (let index = 0; index < 72; index += 1) {
        const x = 22 + ((index * 83) % (width - 44));
        const y = 20 + ((index * 137) % (height - 40));
        const nearNode = Object.values(positions).some((point) =>
          Math.hypot(point.x - x, point.y - y) < 65
        );
        if (nearNode) continue;
        backdropLayer.appendChild(svgElement("circle", {
          cx: x,
          cy: y,
          r: index % 11 === 0 ? 2.1 : index % 4 === 0 ? 1.35 : 0.8,
          class: "kg-star"
        }));
      }
    }

    function radiusFor(id) {
      const kind = nodeData.get(id)?.kind;
      if (kind === "core") return 69;
      if (kind === "tone") return 55;
      if (kind === "pattern") return 51;
      if (kind === "evidence") return 8;
      return 0;
    }

    function curvedPath(fromId, toId, bend) {
      const from = positions[fromId];
      const to = positions[toId];
      const dx = to.x - from.x;
      const dy = to.y - from.y;
      const distance = Math.hypot(dx, dy) || 1;
      const ux = dx / distance;
      const uy = dy / distance;
      const start = {
        x: from.x + ux * radiusFor(fromId),
        y: from.y + uy * radiusFor(fromId)
      };
      const end = {
        x: to.x - ux * radiusFor(toId),
        y: to.y - uy * radiusFor(toId)
      };
      const control = {
        x: (start.x + end.x) / 2 - uy * bend,
        y: (start.y + end.y) / 2 + ux * bend
      };
      return `M${start.x},${start.y} Q${control.x},${control.y} ${end.x},${end.y}`;
    }

    function drawEdge(edge, index) {
      const bend = edge.type === "summary"
        ? ((index % 3) - 1) * 12
        : ((index % 2) ? 18 : -18);
      const path = svgElement("path", {
        d: curvedPath(edge.from, edge.to, bend),
        class: `kg-edge ${edge.type}`,
        "data-from": edge.from,
        "data-to": edge.to
      });
      edgeLayer.appendChild(path);
    }

    function drawNode(node, kind, radius) {
      const point = positions[node.id];
      const questionClass = kind === "core"
        ? ` core-${String(node.question || "").toLowerCase()}`
        : "";
      const isTone = kind === "tone";
      const group = svgElement("g", {
        class: `kg-node ${kind}${questionClass}`,
        "data-id": node.id,
        role: "button",
        tabindex: "0",
        "aria-label": kind === "core"
          ? `人格核心：${node.label}`
          : isTone
            ? `语气原句：${node.tone_exemplars.length} 条逐字样例`
            : `思维模式：${node.label}，${node.evidence_count} 条证据`
      });
      if (isTone) {
        group.appendChild(svgElement("circle", {
          cx: point.x, cy: point.y, r: radius + 10, class: "tone-halo"
        }));
      }
      group.appendChild(svgElement("circle", {
        cx: point.x, cy: point.y, r: radius
      }));

      const kicker = svgElement("text", {
        x: point.x,
        y: point.y - (kind === "core" ? 29 : isTone ? 23 : 22),
        class: "node-kicker"
      });
      kicker.textContent = kind === "core"
        ? "人格核心"
        : isTone ? "原声样例" : "思维模式";
      group.appendChild(kicker);

      const lines = splitLabel(node.label, kind === "core" ? 6 : 7);
      lines.forEach((line, index) => {
        const label = svgElement("text", {
          x: point.x,
          y: point.y - (lines.length - 1) * 9 + index * 19,
          class: "node-label"
        });
        label.textContent = line;
        group.appendChild(label);
      });

      if (kind === "pattern" || isTone) {
        const sub = svgElement("text", {
          x: point.x,
          y: point.y + 30,
          class: "node-sub"
        });
        sub.textContent = isTone
          ? `${node.tone_exemplars.length} 条原句`
          : `${node.evidence_count} 条证据`;
        group.appendChild(sub);
      }

      const activate = () => {
        clearEvidence();
        select(node.id);
        onSelect(nodeData.get(node.id));
      };
      group.addEventListener("click", activate);
      group.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          activate();
        }
      });
      nodeLayer.appendChild(group);
    }

    function select(id) {
      const connected = new Set([id]);
      edgeData.forEach((edge) => {
        if (edge.from === id) connected.add(edge.to);
        if (edge.to === id) connected.add(edge.from);
      });
      svg.querySelectorAll(".kg-node:not(.evidence)").forEach((node) => {
        const nodeId = node.getAttribute("data-id");
        node.classList.toggle("selected", nodeId === id);
        node.classList.toggle("dimmed", !connected.has(nodeId));
      });
      svg.querySelectorAll(".kg-edge:not(.evidence)").forEach((edge) => {
        const active = edge.getAttribute("data-from") === id ||
          edge.getAttribute("data-to") === id;
        edge.classList.toggle("active", active);
        edge.classList.toggle("dimmed", !active);
      });
    }

    function clearEvidence() {
      evidenceEdges.length = 0;
      evidenceLayer.replaceChildren();
    }

    function showEvidence(pattern) {
      clearEvidence();
      const origin = positions[pattern.id];
      const inwardAngle = Math.atan2(center.y - origin.y, center.x - origin.x);
      pattern.evidence.forEach((item, index) => {
        const spread = (index - (pattern.evidence.length - 1) / 2) * 0.24;
        const angle = inwardAngle + spread;
        const distance = 90 + (index % 2) * 16;
        positions[item.id] = {
          x: Math.max(18, Math.min(width - 18, origin.x + Math.cos(angle) * distance)),
          y: Math.max(18, Math.min(height - 18, origin.y + Math.sin(angle) * distance))
        };
        nodeData.set(item.id, {
          kind: "evidence",
          data: item,
          parent: pattern
        });
        evidenceEdges.push({ from: pattern.id, to: item.id });
        const path = svgElement("path", {
          d: curvedPath(pattern.id, item.id, 0),
          class: "kg-edge evidence",
          "data-from": pattern.id,
          "data-to": item.id
        });
        evidenceLayer.appendChild(path);
        const group = svgElement("g", {
          class: "kg-node evidence",
          "data-id": item.id,
          role: "button",
          tabindex: "0",
          "aria-label": `${item.paper}，${item.observation}`
        });
        group.appendChild(svgElement("circle", {
          cx: positions[item.id].x,
          cy: positions[item.id].y,
          r: 8
        }));
        const activate = () => {
          selectEvidence(item.id);
          onSelect(nodeData.get(item.id));
        };
        group.addEventListener("click", activate);
        group.addEventListener("keydown", (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            activate();
          }
        });
        evidenceLayer.appendChild(group);
      });
      select(pattern.id);
    }

    function selectEvidence(id) {
      svg.querySelectorAll(".kg-node").forEach((node) => {
        node.classList.toggle("selected", node.getAttribute("data-id") === id);
        node.classList.remove("dimmed");
      });
      svg.querySelectorAll(".kg-edge").forEach((edge) => {
        const active = edge.getAttribute("data-to") === id;
        edge.classList.toggle("active", active);
        edge.classList.toggle("dimmed", !active);
      });
    }

    const initial = model.cores.find((item) => item.question === "P03") || model.cores[0];
    select(initial.id);
    return { select, showEvidence, clearEvidence };
  };
})();
