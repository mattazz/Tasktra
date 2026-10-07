(function relationshipMapModule(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.TasktraRelationshipMap = api;
})(typeof window !== "undefined" ? window : globalThis, function relationshipMapFactory() {
  "use strict";

  const MAX_VISIBLE_NODES = 250;
  const typeOrder = { goal: 0, job: 1, agent: 2, "stub-goal": 3, "stub-job": 4, "unresolved-parent": 5 };
  const nodeId = (type, id) => `${type}:${String(id)}`;
  const agentKey = (agent) => agent && (agent.work_id || agent.id);
  const text = (value) => String(value == null ? "" : value);
  const ordered = (items) => [...items].sort((left, right) =>
    (typeOrder[left.type] ?? 99) - (typeOrder[right.type] ?? 99) ||
    text(left.label).localeCompare(text(right.label)) || text(left.id).localeCompare(text(right.id))
  );

  function buildGraph(snapshot) {
    const source = snapshot && typeof snapshot === "object" ? snapshot : {};
    const nodes = [];
    const edges = [];
    const issues = [];
    const byId = new Map();
    const addNode = (node) => {
      if (byId.has(node.id)) return byId.get(node.id);
      const value = { missing: false, relationId: null, record: null, ...node };
      byId.set(value.id, value);
      nodes.push(value);
      return value;
    };
    const addEdge = (sourceId, targetId, type) => {
      if (!byId.has(sourceId) || !byId.has(targetId)) return;
      const id = `${type}:${sourceId}->${targetId}`;
      if (!edges.some((edge) => edge.id === id)) edges.push({ id, source: sourceId, target: targetId, type });
    };
    const getStub = (type, id) => {
      const kind = type === "goal" ? "stub-goal" : "stub-job";
      return addNode({
        id: nodeId(kind, id), type: kind, missing: true, relationId: text(id),
        label: `${type === "goal" ? "Goal" : "Job"} not loaded · ${text(id)}`,
      });
    };
    const getGoal = (id) => byId.get(nodeId("goal", id)) || getStub("goal", id);
    const getJob = (id) => byId.get(nodeId("job", id)) || getStub("job", id);

    (Array.isArray(source.goals) ? source.goals : []).forEach((record) => {
      if (!record || record.id == null) return;
      addNode({ id: nodeId("goal", record.id), type: "goal", record, relationId: text(record.id), label: record.title || text(record.id) });
    });
    (Array.isArray(source.jobs) ? source.jobs : []).forEach((record) => {
      if (!record || record.id == null) return;
      const job = addNode({ id: nodeId("job", record.id), type: "job", record, relationId: text(record.id), label: record.title || text(record.id) });
      if (record.goal_id != null && text(record.goal_id)) addEdge(getGoal(record.goal_id).id, job.id, "goal-job");
    });
    (Array.isArray(source.agents) ? source.agents : []).forEach((record) => {
      const key = agentKey(record);
      if (!record || key == null || !text(key)) return;
      addNode({ id: nodeId("agent", key), type: "agent", record, relationId: text(key), label: record.role || record.id || text(key) });
    });
    (Array.isArray(source.agents) ? source.agents : []).forEach((record) => {
      const key = agentKey(record);
      if (!record || key == null || !text(key)) return;
      const agent = byId.get(nodeId("agent", key));
      const linkedJob = record.job_id != null && text(record.job_id) ? getJob(record.job_id) : null;
      if (linkedJob) addEdge(linkedJob.id, agent.id, "job-agent");
      const goalAlreadyReachedThroughJob = linkedJob && linkedJob.record && text(linkedJob.record.goal_id) === text(record.goal_id);
      if (record.goal_id != null && text(record.goal_id) && !goalAlreadyReachedThroughJob) addEdge(getGoal(record.goal_id).id, agent.id, "goal-agent");
      if (record.parent_work_id != null && text(record.parent_work_id)) {
        const parentId = text(record.parent_work_id);
        const parent = byId.get(nodeId("agent", parentId)) || (text(record.job_id) === parentId ? byId.get(nodeId("job", parentId)) : null);
        if (parent) addEdge(parent.id, agent.id, "parent-lineage");
        else {
          const unresolved = addNode({
            id: nodeId("unresolved-parent", parentId), type: "unresolved-parent", missing: true,
            relationId: parentId, label: `Parent work not loaded · ${parentId}`,
          });
          issues.push({ type: "parent-work-not-loaded", id: parentId, message: `Parent work ${parentId} is not loaded.` });
          addEdge(unresolved.id, agent.id, "parent-lineage");
        }
      }
    });
    return { nodes: ordered(nodes), edges: [...edges].sort((a, b) => a.id.localeCompare(b.id)), issues };
  }

  function filterGraph(graph, options = {}) {
    const full = graph && Array.isArray(graph.nodes) ? graph : { nodes: [], edges: [] };
    const query = text(options.query).trim().toLowerCase();
    const focusId = text(options.focusId).trim();
    const maxNodes = Number.isFinite(options.maxNodes) ? Math.max(1, Math.floor(options.maxNodes)) : MAX_VISIBLE_NODES;
    const nodes = ordered(full.nodes);
    const nodeById = new Map(nodes.map((node) => [node.id, node]));
    const adjacent = new Map(nodes.map((node) => [node.id, new Set()]));
    (full.edges || []).forEach((edge) => {
      if (!nodeById.has(edge.source) || !nodeById.has(edge.target)) return;
      adjacent.get(edge.source).add(edge.target);
      adjacent.get(edge.target).add(edge.source);
    });
    const matching = query ? nodes.filter((node) => [node.label, node.id, node.relationId, node.record && node.record.id, node.record && node.record.role, node.record && node.record.title].some((value) => text(value).toLowerCase().includes(query))) : [];
    const seedIds = focusId && nodeById.has(focusId) ? [focusId] : matching.map((node) => node.id);
    const selected = new Set();
    if (seedIds.length) {
      seedIds.forEach((id) => {
        selected.add(id);
        (adjacent.get(id) || []).forEach((neighbor) => selected.add(neighbor));
      });
    } else if (!query && !focusId) {
      nodes.forEach((node) => selected.add(node.id));
    }
    const seedSet = new Set(seedIds);
    const priority = [
      ...seedIds.map((id) => nodeById.get(id)).filter(Boolean),
      ...ordered([...selected].filter((id) => !seedSet.has(id)).map((id) => nodeById.get(id)).filter(Boolean)),
    ];
    const limitedIds = new Set(priority.slice(0, maxNodes).map((node) => node.id));
    const visibleNodes = priority.filter((node) => limitedIds.has(node.id));
    const visibleEdges = (full.edges || []).filter((edge) => limitedIds.has(edge.source) && limitedIds.has(edge.target));
    return {
      nodes: visibleNodes,
      edges: visibleEdges,
      meta: {
        totalNodes: nodes.length, totalEdges: (full.edges || []).length,
        loadedRecords: nodes.filter((node) => !node.missing).length,
        missingReferences: nodes.filter((node) => node.missing).length,
        filteredNodes: selected.size, filteredEdges: (full.edges || []).filter((edge) => selected.has(edge.source) && selected.has(edge.target)).length,
        visibleNodes: visibleNodes.length, visibleEdges: visibleEdges.length,
        truncated: selected.size > visibleNodes.length, hiddenNodes: Math.max(0, selected.size - visibleNodes.length),
        query, matchedNodeIds: matching.map((node) => node.id), scopeKey: text(options.scopeKey), focusId,
      },
    };
  }

  function layoutGraph(graph) {
    const input = graph && Array.isArray(graph.nodes) ? graph : { nodes: [] };
    const groups = { goal: [], job: [], agent: [], other: [] };
    ordered(input.nodes).forEach((node) => {
      const group = node.type === "goal" || node.type === "stub-goal" ? "goal" : node.type === "job" || node.type === "stub-job" ? "job" : node.type === "agent" ? "agent" : "other";
      groups[group].push(node);
    });
    const layers = ["goal", "job", "agent", "other"];
    const rowGap = 72, maxRows = 12, columnGap = 142;
    const positioned = [];
    let x = 115;
    layers.forEach((key) => {
      const records = groups[key], columns = Math.max(1, Math.ceil(records.length / maxRows));
      if (!records.length) return;
      records.forEach((node, index) => positioned.push({ ...node, x: x + Math.floor(index / maxRows) * columnGap, y: 82 + (index % maxRows) * rowGap }));
      x += columns * columnGap + 130;
    });
    if (!positioned.length) return { nodes: positioned, bounds: { x: 0, y: 0, width: 1, height: 1 } };
    const left = Math.min(...positioned.map((node) => node.x - 72));
    const right = Math.max(...positioned.map((node) => node.x + 72));
    const top = Math.min(...positioned.map((node) => node.y - 45));
    const bottom = Math.max(...positioned.map((node) => node.y + 45));
    return { nodes: positioned, bounds: { x: left, y: top, width: right - left + 140, height: bottom - top } };
  }

  function create(element, options = {}) {
    if (!element || !element.ownerDocument) throw new Error("A map host element is required.");
    const doc = element.ownerDocument;
    const svgNs = "http://www.w3.org/2000/svg";
    const make = (tag, className, value) => { const node = doc.createElement(tag); if (className) node.className = className; if (value != null) node.textContent = value; return node; };
    const makeSvg = (tag, className) => { const node = doc.createElementNS(svgNs, tag); if (className) node.setAttribute("class", className); return node; };
    const root = make("div", "relationship-map");
    const toolbar = make("div", "relationship-map-toolbar");
    const searchLabel = make("label", "relationship-map-search");
    searchLabel.htmlFor = "relationship-map-search-input";
    searchLabel.append(make("span", "sr-only", "Search the relationship map"));
    const search = make("input", ""); search.type = "search"; search.id = "relationship-map-search-input"; search.placeholder = "Search goals, jobs, agents, or references"; search.autocomplete = "off"; searchLabel.append(search);
    const pickerLabel = make("label", "relationship-map-picker"); pickerLabel.append(make("span", "sr-only", "Jump to a loaded map record"));
    const picker = make("select", ""); picker.id = "relationship-map-node-picker"; picker.setAttribute("aria-label", "Jump to a map node"); const pickerPlaceholder = make("option", "", "Jump to a map node"); pickerPlaceholder.value = ""; picker.append(pickerPlaceholder); pickerLabel.append(picker);
    const focusButton = make("button", "text-button", "Focus connections"); focusButton.type = "button"; focusButton.disabled = true; focusButton.setAttribute("aria-pressed", "false");
    const zoomOut = make("button", "icon-button", "−"); zoomOut.type = "button"; zoomOut.setAttribute("aria-label", "Zoom out");
    const zoomIn = make("button", "icon-button", "+"); zoomIn.type = "button"; zoomIn.setAttribute("aria-label", "Zoom in");
    const fitButton = make("button", "text-button", "Fit view"); fitButton.type = "button";
    toolbar.append(searchLabel, pickerLabel, focusButton, zoomOut, zoomIn, fitButton);
    const summary = make("p", "relationship-map-summary"); summary.setAttribute("role", "status"); summary.setAttribute("aria-live", "polite");
    const legend = make("ul", "relationship-map-legend");
    [["goal", "Goal"], ["job", "Job"], ["agent", "Agent"], ["goal-job", "Goal → job"], ["job-agent", "Job → agent"], ["direct", "Goal membership"], ["missing", "Not loaded"], ["lineage", "Parent → child lineage"]].forEach(([kind, label]) => { const item = make("li", `relationship-map-legend-${kind}`); item.append(make("span", "", ""), make("span", "", label)); legend.append(item); });
    const workspace = make("div", "relationship-map-workspace");
    const mapWrap = make("div", "relationship-map-canvas");
    const svg = makeSvg("svg", "relationship-map-svg"); svg.setAttribute("viewBox", "0 0 1200 640"); svg.setAttribute("role", "group"); svg.setAttribute("aria-label", "Interactive relationship map. Use search, record picker, map node buttons, and map controls to explore recorded relationships."); svg.setAttribute("tabindex", "0");
    const defs = makeSvg("defs", ""), arrow = makeSvg("marker", "relationship-map-lineage-arrow"); arrow.setAttribute("id", "relationship-map-lineage-arrow"); arrow.setAttribute("viewBox", "0 0 10 10"); arrow.setAttribute("refX", "9"); arrow.setAttribute("refY", "5"); arrow.setAttribute("markerWidth", "6"); arrow.setAttribute("markerHeight", "6"); arrow.setAttribute("orient", "auto-start-reverse"); const arrowShape = makeSvg("path", ""); arrowShape.setAttribute("d", "M 0 0 L 10 5 L 0 10 z"); arrow.append(arrowShape); defs.append(arrow);
    const scene = makeSvg("g", "relationship-map-scene"); const edgeLayer = makeSvg("g", "relationship-map-edges"); const nodeLayer = makeSvg("g", "relationship-map-nodes"); scene.append(edgeLayer, nodeLayer); svg.append(defs, scene); mapWrap.append(svg);
    const details = make("aside", "relationship-map-details"); details.setAttribute("aria-live", "polite");
    workspace.append(mapWrap, details); root.append(toolbar, summary, legend, workspace); element.replaceChildren(root);

    let graph = { nodes: [], edges: [], issues: [] }, filtered = { nodes: [], edges: [], meta: {} }, layout = { nodes: [], bounds: { x: 0, y: 0, width: 1, height: 1 } };
    let selectedId = "", focusId = "", view = { x: 30, y: 25, scale: 0.7 }, dragging = null;
    const bounds = () => ({ width: 1200, height: 640 });
    const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
    const refreshTransform = () => scene.setAttribute("transform", `translate(${view.x} ${view.y}) scale(${view.scale})`);
    const connected = () => {
      if (!selectedId) return new Set();
      const result = new Set([selectedId]);
      (filtered.edges || []).forEach((edge) => { if (edge.source === selectedId) result.add(edge.target); if (edge.target === selectedId) result.add(edge.source); });
      return result;
    };
    const fit = () => {
      const b = layout.bounds || { x: 0, y: 0, width: 1, height: 1 }, viewport = bounds();
      view.scale = clamp(Math.min((viewport.width - 90) / Math.max(1, b.width), (viewport.height - 90) / Math.max(1, b.height)), 0.03, 1.25);
      view.x = (viewport.width - b.width * view.scale) / 2 - b.x * view.scale;
      view.y = Math.max(25, (viewport.height - b.height * view.scale) / 2 - b.y * view.scale);
      refreshTransform();
    };
    const nodeDetails = () => {
      const node = graph.nodes.find((item) => item.id === selectedId);
      const record = node && node.record || {};
      const relationKey = node ? (graph.edges || []).filter((edge) => edge.source === node.id || edge.target === node.id).map((edge) => { const other = graph.nodes.find((item) => item.id === (edge.source === node.id ? edge.target : edge.source)); return `${edge.id}:${edge.source === node.id ? "out" : "in"}:${other && other.label}:${other && other.missing}`; }).join("|") : "";
      const detailKey = node ? [node.id, node.label, node.missing, record.state, record.status, record.outcome, record.observed_model || record.model, record.observed_effort || record.effort, record.total_tokens, relationKey].join("|") : "empty";
      if (details.dataset.detailKey === detailKey) return;
      const restoreOpenFocus = details.contains(doc.activeElement) && doc.activeElement.classList.contains("relationship-map-open");
      details.dataset.detailKey = detailKey; details.replaceChildren();
      if (!node) { details.append(make("p", "relationship-map-empty", "Choose a node to inspect its recorded relationships.")); return; }
      details.append(make("p", "eyebrow", node.missing ? "Reference not loaded" : `${node.type} record`), make("h3", "", node.label));
      const grid = make("div", "relationship-map-detail-grid");
      const field = (label, value) => { const item = make("div", "detail-item"); item.append(make("b", "", label), make("span", "", value || "Unavailable")); grid.append(item); };
      field("Record ID", node.relationId || node.id);
      field("Known state", record.state || record.status || record.outcome || (node.missing ? "Not loaded" : "Unavailable"));
      field("Model / effort", [record.observed_model || record.model, record.observed_effort || record.effort].filter(Boolean).join(" · ") || "Unavailable");
      field("Tokens", Number.isFinite(record.total_tokens) ? new Intl.NumberFormat().format(record.total_tokens) : "Unavailable");
      const relations = (graph.edges || []).filter((edge) => edge.source === node.id || edge.target === node.id).map((edge) => {
        const other = graph.nodes.find((item) => item.id === (edge.source === node.id ? edge.target : edge.source));
        if (edge.type === "parent-lineage") { const anchor = edge.source.startsWith("job:"); return edge.source === node.id ? `${anchor ? "Job anchor" : "Parent execution"} → child execution · ${other ? other.label : "not loaded"}` : `Child execution ← ${anchor ? "job anchor" : "parent execution"} · ${other ? other.label : "not loaded"}`; }
        return `${edge.type.replace(/-/g, " ")} · ${other ? other.label : "not loaded"}`;
      });
      field("Explicit relations", relations.length ? relations.join("\n") : "None recorded");
      details.append(grid);
      if (node.record && !node.missing) { const open = make("button", "button button-accent relationship-map-open", "Open record"); open.type = "button"; open.addEventListener("click", () => { const currentNode = graph.nodes.find((item) => item.id === node.id); if (currentNode && currentNode.record && typeof options.onOpen === "function") options.onOpen({ type: currentNode.type, record: currentNode.record }); }); details.append(open); if (restoreOpenFocus) open.focus({ preventScroll: true }); }
    };
    const nodeRadius = (node) => node.type === "agent" ? 34 : node.missing ? 42 : 62;
    const edgePath = (source, target) => {
      const sourceRadius = nodeRadius(source), targetRadius = nodeRadius(target);
      if (source.id === target.id) return `M ${source.x} ${source.y - sourceRadius} C ${source.x + 116} ${source.y - 124}, ${source.x + 116} ${source.y + 124}, ${source.x} ${source.y + sourceRadius}`;
      if (source.x === target.x) return `M ${source.x + sourceRadius} ${source.y} C ${source.x + 128} ${source.y}, ${target.x + 128} ${target.y}, ${target.x + targetRadius} ${target.y}`;
      const direction = target.x > source.x ? 1 : -1;
      const startX = source.x + direction * sourceRadius, endX = target.x - direction * targetRadius;
      return `M ${startX} ${source.y} C ${startX + (endX - startX) * .42} ${source.y}, ${endX - (endX - startX) * .42} ${target.y}, ${endX} ${target.y}`;
    };
    function syncSelectionControls() {
      if (!graph.nodes.some((node) => node.id === selectedId)) selectedId = "";
      if (!selectedId) focusId = "";
      picker.value = selectedId;
      focusButton.disabled = !selectedId;
      focusButton.textContent = focusId ? "Show all nodes" : "Focus connections";
      focusButton.setAttribute("aria-pressed", String(Boolean(focusId)));
    }
    const selectNode = (id) => { selectedId = id; syncSelectionControls(); nodeDetails(); draw(); };
    const draw = () => {
      const focusedNodeId = nodeLayer.contains(doc.activeElement) ? doc.activeElement.dataset.nodeId : "";
      layout = layoutGraph(filtered);
      const positions = new Map(layout.nodes.map((node) => [node.id, node]));
      const highlighted = connected();
      const selectedVisible = filtered.nodes.some((node) => node.id === selectedId);
      edgeLayer.replaceChildren(); nodeLayer.replaceChildren();
      filtered.edges.forEach((edge) => {
        const source = positions.get(edge.source), target = positions.get(edge.target); if (!source || !target) return;
        const path = makeSvg("path", `relationship-map-edge relationship-map-edge-${edge.type}${selectedVisible && !(highlighted.has(edge.source) && highlighted.has(edge.target)) ? " is-muted" : ""}`);
        path.setAttribute("d", edgePath(source, target)); path.dataset.source = edge.source; path.dataset.target = edge.target; path.dataset.relation = edge.type; if (edge.type === "parent-lineage") path.setAttribute("marker-end", "url(#relationship-map-lineage-arrow)"); path.setAttribute("aria-hidden", "true"); edgeLayer.append(path);
      });
      layout.nodes.forEach((node) => {
        const group = makeSvg("g", `relationship-map-node relationship-map-node-${node.type}${node.id === selectedId ? " is-selected" : ""}${selectedVisible && !highlighted.has(node.id) ? " is-muted" : ""}`);
        group.setAttribute("transform", `translate(${node.x} ${node.y})`); group.setAttribute("role", "button"); group.setAttribute("tabindex", "0"); group.dataset.nodeId = node.id; group.setAttribute("aria-label", `${node.type.replace(/-/g, " ")} ${node.label}, record ${node.relationId || node.id}${node.missing ? ", not loaded" : ""}.`);
        let shape;
        if (node.type === "agent") { shape = makeSvg("circle", "relationship-map-shape"); shape.setAttribute("r", "34"); }
        else if (node.missing) { shape = makeSvg("path", "relationship-map-shape"); shape.setAttribute("d", "M 0 -37 L 42 0 L 0 37 L -42 0 Z"); }
        else { shape = makeSvg("rect", "relationship-map-shape"); shape.setAttribute("x", "-62"); shape.setAttribute("y", "-27"); shape.setAttribute("width", "124"); shape.setAttribute("height", "54"); shape.setAttribute("rx", node.type === "goal" ? "18" : "5"); }
        const label = makeSvg("text", "relationship-map-node-label"); label.setAttribute("text-anchor", "middle"); label.setAttribute("y", node.type === "agent" ? "5" : "4"); label.textContent = text(node.label).slice(0, 21);
        const title = makeSvg("title", ""); title.textContent = `${node.label} · ${node.relationId || node.id}`; group.append(title, shape, label);
        const activate = (event) => { event.preventDefault(); selectNode(node.id); };
        group.addEventListener("click", activate); group.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") activate(event); }); nodeLayer.append(group);
      });
      refreshTransform();
      if (focusedNodeId) nodeLayer.querySelector(`[data-node-id="${typeof CSS !== "undefined" && CSS.escape ? CSS.escape(focusedNodeId) : focusedNodeId.replace(/"/g, "\\\"")}"]`)?.focus({ preventScroll: true });
    };
    const refresh = ({ fitView = false } = {}) => {
      filtered = filterGraph(graph, { query: search.value, focusId, scopeKey: filtered.meta && filtered.meta.scopeKey, maxNodes: MAX_VISIBLE_NODES });
      const meta = filtered.meta;
      const message = `${meta.loadedRecords} loaded record${meta.loadedRecords === 1 ? "" : "s"} and ${meta.missingReferences} not-loaded reference${meta.missingReferences === 1 ? "" : "s"} across ${meta.totalNodes} map node${meta.totalNodes === 1 ? "" : "s"} and ${meta.totalEdges} recorded connection${meta.totalEdges === 1 ? "" : "s"}. ${meta.filteredNodes} match this view; ${meta.visibleNodes} shown.${meta.truncated ? ` ${meta.hiddenNodes} matching node${meta.hiddenNodes === 1 ? "" : "s"} are outside the 250-node map limit. Search or focus a record to narrow the view.` : ""}${search.value.trim() && !meta.matchedNodeIds.length ? " No map nodes match this search." : ""}`;
      if (summary.textContent !== message) summary.textContent = message;
      draw(); if (fitView) fit(); nodeDetails();
    };
    const syncPicker = (clear = false) => { const previous = clear ? "" : picker.value, restoreFocus = doc.activeElement === picker, placeholder = make("option", "", "Jump to a map node"); placeholder.value = ""; picker.replaceChildren(placeholder); ordered(graph.nodes).forEach((node) => { const option = make("option", "", `${node.type.replace(/-/g, " ")} · ${node.label} · ${node.relationId || node.id}`); option.value = node.id; picker.append(option); }); picker.value = graph.nodes.some((node) => node.id === previous) ? previous : ""; if (restoreFocus) picker.focus({ preventScroll: true }); };
    const zoom = (factor) => { view.scale = clamp(view.scale * factor, 0.03, 3); refreshTransform(); };
    search.addEventListener("input", () => { selectedId = ""; focusId = ""; syncSelectionControls(); refresh({ fitView: true }); });
    picker.addEventListener("change", () => { if (!picker.value) return; search.value = ""; selectedId = picker.value; focusId = selectedId; syncSelectionControls(); refresh({ fitView: true }); });
    focusButton.addEventListener("click", () => { if (!selectedId) return; search.value = ""; focusId = focusId ? "" : selectedId; syncSelectionControls(); refresh({ fitView: true }); });
    zoomIn.addEventListener("click", () => zoom(1.22)); zoomOut.addEventListener("click", () => zoom(1 / 1.22)); fitButton.addEventListener("click", fit);
    svg.addEventListener("wheel", (event) => { event.preventDefault(); zoom(event.deltaY < 0 ? 1.12 : 1 / 1.12); }, { passive: false });
    const mapPoint = (event) => { const point = svg.createSVGPoint(), matrix = svg.getScreenCTM(); point.x = event.clientX; point.y = event.clientY; return matrix ? point.matrixTransform(matrix.inverse()) : { x: event.clientX, y: event.clientY }; };
    svg.addEventListener("pointerdown", (event) => { if (event.target.closest && event.target.closest(".relationship-map-node")) return; const point = mapPoint(event); dragging = { x: point.x, y: point.y, startX: view.x, startY: view.y }; svg.setPointerCapture(event.pointerId); });
    svg.addEventListener("pointermove", (event) => { if (!dragging) return; const point = mapPoint(event); view.x = dragging.startX + point.x - dragging.x; view.y = dragging.startY + point.y - dragging.y; refreshTransform(); });
    svg.addEventListener("pointerup", () => { dragging = null; }); svg.addEventListener("pointercancel", () => { dragging = null; });

    return {
      update(snapshot, updateOptions = {}) {
        const reset = Boolean(updateOptions.reset);
        graph = buildGraph(snapshot);
        if (reset) { selectedId = ""; focusId = ""; search.value = ""; }
        if (!graph.nodes.some((node) => node.id === selectedId)) { selectedId = ""; focusId = ""; }
        filtered = { meta: { scopeKey: text(updateOptions.scopeKey) } };
        syncPicker(reset); syncSelectionControls();
        refresh({ fitView: reset || !layout.nodes.length });
      },
      destroy() { element.replaceChildren(); },
    };
  }

  return { buildGraph, filterGraph, layoutGraph, create };
});
