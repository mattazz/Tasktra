(function(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.TasktraRelationshipMap = api;
})(typeof window !== "undefined" ? window : globalThis, function() {
  "use strict";
  const MAX_VISIBLE_NODES = 250;
  const TYPES = { goal: 0, job: 1, agent: 2, "agent-group": 3, "stub-goal": 4, "stub-job": 5, "unresolved-parent": 6 };
  const TERMINAL = new Set(["completed", "complete", "succeeded", "success", "failed", "cancelled", "canceled", "finished", "stopped", "exited"]);
  const ACTIVE = new Set(["active", "queued", "pending", "leased", "started", "running", "working", "in-progress", "in_progress"]);
  const FAILED = new Set(["failed", "failure", "error", "errored"]);
  const text = (v) => String(v == null ? "" : v), id = (type, value) => `${type}:${String(value)}`;
  const agentKey = (record) => record && (record.work_id || record.id);
  const sort = (items) => [...items].sort((a, b) => (TYPES[a.type] ?? 99) - (TYPES[b.type] ?? 99) || text(a.label).localeCompare(text(b.label)) || text(a.id).localeCompare(text(b.id)));
  const groupId = (anchor, role) => `agent-group:${encodeURIComponent(anchor)}:${encodeURIComponent(role || "unspecified")}`;
  const number = (value) => new Intl.NumberFormat().format(value);
  const shortId = (value) => { const source = text(value); return source.length <= 9 ? source : source.slice(-8); };
  const runLabel = (record, jobs) => { const job = record?.job_id != null ? jobs.get(text(record.job_id)) : null, name = job?.title || record?.job_title || record?.title; return name ? `${text(name)} \u00b7 ${shortId(record.work_id || record.id)}` : `${text(record?.role || "run")} \u00b7 ${shortId(record?.work_id || record?.id)}`; };
  const READABLE_MIN_ZOOM = .84;
  const NODE_PRESENTATION = {
    goal: { width: 272, height: 118, textMaxWidth: 228 },
    job: { width: 256, height: 110, textMaxWidth: 216 },
    "job-group": { width: 244, height: 102, textMaxWidth: 204 },
    agent: { width: 268, height: 112, textMaxWidth: 228 },
    "agent-group": { width: 236, height: 102, textMaxWidth: 196 },
    missing: { width: 212, height: 112, textMaxWidth: 166 }
  };
  const ellipsis = (value, limit) => { const source = text(value).trim(); return source.length <= limit ? source : `${source.slice(0, Math.max(1, limit - 1)).trimEnd()}…`; };
  const visualUnits = (value) => [...text(value)].reduce((total, character) => total + (character.codePointAt(0) > 0xffff ? 1.55 : /[\u2E80-\u9FFF\uF900-\uFAFF]/.test(character) ? 1.9 : /[WM@#%&]/.test(character) ? 1.65 : /[A-Z]/.test(character) ? 1.25 : 1), 0);
  const visualEllipsis = (value, limit) => {
    const source = text(value).trim(), safe = Math.max(1, Number(limit) || 1);
    if (visualUnits(source) <= safe) return source;
    const budget = Math.max(.25, safe - 1), chars = [];
    let used = 0;
    for (const character of source) { const next = visualUnits(character); if (used + next > budget) break; chars.push(character); used += next; }
    return `${chars.join("").trimEnd() || source.slice(0, 1)}…`;
  };
  // Cytoscape labels need explicit line breaks: its word wrap has no line-count cap,
  // which can otherwise put a correct label outside a fixed-size node.
  const wrapNodeLabel = (value, lineLength = 25, maxLines = 3) => {
    const words = text(value).replace(/\s+/g, " ").trim().split(" ").filter(Boolean);
    if (!words.length) return "Unnamed record";
    const lines = [], safeLength = Math.max(1, Number(lineLength) || 1), safeLines = Math.max(1, Math.floor(maxLines));
    let line = "";
    for (const original of words) {
      let word = original;
      while (word) {
        if (!line && visualUnits(word) > safeLength) {
          let part = "", used = 0;
          for (const character of word) { const next = visualUnits(character); if (used + next > safeLength) break; part += character; used += next; }
          lines.push(part || word.slice(0, 1)); word = word.slice(part.length || 1); continue;
        }
        const candidate = line ? `${line} ${word}` : word;
        if (visualUnits(candidate) <= safeLength) { line = candidate; word = ""; continue; }
        lines.push(line); line = "";
      }
    }
    if (line) lines.push(line);
    return lines.length <= safeLines ? lines.join("\n") : `${lines.slice(0, safeLines - 1).join("\n")}${safeLines > 1 ? "\n" : ""}${visualEllipsis(lines.slice(safeLines - 1).join(" "), safeLength)}`;
  };
  const nodePresentation = (node) => {
    const type = node?.missing ? "missing" : node?.type;
    const metrics = NODE_PRESENTATION[type] || NODE_PRESENTATION.job;
    if (type === "agent") {
      const record = node.record || {}, role = visualEllipsis(record.role || "run", 14), model = visualEllipsis(record.observed_model || record.model || "unknown model", 12), run = shortId(record.work_id || record.id || node.relationId || node.id);
      return { ...metrics, label: `RUN\n${role}\n${run} · ${model}` };
    }
    const category = type === "goal" ? "GOAL" : type === "job" ? "JOB" : type === "job-group" ? "JOB GROUP" : type === "agent-group" ? "RUN GROUP" : "REFERENCE";
    return { ...metrics, label: `${category}\n${wrapNodeLabel(node?.label, type === "goal" ? 21 : type === "missing" ? 15 : 20, 2)}` };
  };
  const layoutKey = (projectKey, scopeKey, preset, demo) => `tasktra.map.positions.v1:${encodeURIComponent(text(projectKey || "project"))}:${encodeURIComponent(text(scopeKey || "all"))}:${preset === "active" || preset === "failed" ? preset : "all"}:${demo ? "demo" : "live"}`;
  const validPositions = (value, allowed) => { const result = new Map(); if (!value || typeof value !== "object" || value.version !== 1 || !Array.isArray(value.positions) || value.positions.length > MAX_VISIBLE_NODES) return result; value.positions.forEach((entry) => { if (!Array.isArray(entry) || entry.length !== 3 || !allowed.has(text(entry[0])) || !Number.isFinite(entry[1]) || !Number.isFinite(entry[2]) || Math.abs(entry[1]) > 100000 || Math.abs(entry[2]) > 100000) return; result.set(text(entry[0]), { x: entry[1], y: entry[2] }); }); return result; };
  function presetGraph(graph, preset) {
    if (preset !== "active" && preset !== "failed") return graph;
    const nodes = Array.isArray(graph?.nodes) ? graph.nodes : [], edges = Array.isArray(graph?.edges) ? graph.edges : [];
    const targets = new Set(nodes.filter((node) => { if (node.type !== "agent") return false; const state = text(node.record?.state || node.record?.status || node.record?.outcome).toLowerCase(); return preset === "active" ? ACTIVE.has(state) : FAILED.has(state); }).map((node) => node.id));
    const contexts = new Set(targets), typeById = new Map(nodes.map((node) => [node.id, node.type]));
    let changed = true;
    while (changed) { changed = false; edges.forEach((edge) => { const left = contexts.has(edge.source), right = contexts.has(edge.target); if (left === right) return; const other = left ? edge.target : edge.source, type = typeById.get(other); if (type === "goal" || type === "job" || type === "stub-goal" || type === "stub-job") { contexts.add(other); changed = true; } }); }

    return { ...graph, nodes: nodes.filter((node) => contexts.has(node.id)), edges: edges.filter((edge) => contexts.has(edge.source) && contexts.has(edge.target)) };
  }

  function buildGraph(snapshot) {
    const input = snapshot && typeof snapshot === "object" ? snapshot : {}, nodes = [], edges = [], issues = [], byId = new Map();
    const addNode = (node) => { if (byId.has(node.id)) return byId.get(node.id); const item = { missing: false, relationId: null, record: null, ...node }; byId.set(item.id, item); nodes.push(item); return item; };
    const addEdge = (source, target, type) => { if (!byId.has(source) || !byId.has(target)) return; const edgeId = `${type}:${source}->${target}`; if (!edges.some((edge) => edge.id === edgeId)) edges.push({ id: edgeId, source, target, type }); };
    const stub = (kind, value) => addNode({ id: id(kind === "goal" ? "stub-goal" : "stub-job", value), type: kind === "goal" ? "stub-goal" : "stub-job", missing: true, relationId: text(value), label: `${kind === "goal" ? "Goal" : "Job"} not loaded · ${text(value)}` });
    const goal = (value) => byId.get(id("goal", value)) || stub("goal", value), job = (value) => byId.get(id("job", value)) || stub("job", value);
    (Array.isArray(input.goals) ? input.goals : []).forEach((record) => { if (record && record.id != null) addNode({ id: id("goal", record.id), type: "goal", relationId: text(record.id), label: record.title || text(record.id), record }); });
    (Array.isArray(input.jobs) ? input.jobs : []).forEach((record) => { if (!record || record.id == null) return; const current = addNode({ id: id("job", record.id), type: "job", relationId: text(record.id), label: record.title || text(record.id), record }); if (record.goal_id != null && text(record.goal_id)) addEdge(goal(record.goal_id).id, current.id, "goal-job"); });
    const jobsById = new Map((Array.isArray(input.jobs) ? input.jobs : []).filter((record) => record && record.id != null).map((record) => [text(record.id), record]));
    (Array.isArray(input.agents) ? input.agents : []).forEach((record) => { const key = agentKey(record); if (record && key != null && text(key)) addNode({ id: id("agent", key), type: "agent", relationId: text(key), label: runLabel(record, jobsById), record }); });
    (Array.isArray(input.agents) ? input.agents : []).forEach((record) => {
      const key = agentKey(record); if (!record || key == null || !text(key)) return;
      const current = byId.get(id("agent", key)), linkedJob = record.job_id != null && text(record.job_id) ? job(record.job_id) : null;
      if (linkedJob) addEdge(linkedJob.id, current.id, "job-agent");
      if (record.goal_id != null && text(record.goal_id) && !(linkedJob && linkedJob.record && text(linkedJob.record.goal_id) === text(record.goal_id))) addEdge(goal(record.goal_id).id, current.id, "goal-agent");
      if (record.parent_work_id != null && text(record.parent_work_id)) {
        const parentId = text(record.parent_work_id), parent = byId.get(id("agent", parentId)) || (text(record.job_id) === parentId ? byId.get(id("job", parentId)) : null);
        if (parent) addEdge(parent.id, current.id, "parent-lineage");
        else { const missing = addNode({ id: id("unresolved-parent", parentId), type: "unresolved-parent", missing: true, relationId: parentId, label: `Parent work not loaded · ${parentId}` }); issues.push({ type: "parent-work-not-loaded", id: parentId, message: `Parent work ${parentId} is not loaded.` }); addEdge(missing.id, current.id, "parent-lineage"); }
      }
    });
    return { nodes: sort(nodes), edges: edges.sort((a, b) => a.id.localeCompare(b.id)), issues };
  }

  function filterGraph(graph, options = {}) {
    const full = graph && Array.isArray(graph.nodes) ? graph : { nodes: [], edges: [] }, query = text(options.query).trim().toLowerCase(), focusId = text(options.focusId).trim();
    const maxNodes = Number.isFinite(options.maxNodes) ? Math.max(1, Math.floor(options.maxNodes)) : MAX_VISIBLE_NODES, nodes = sort(full.nodes), byId = new Map(nodes.map((node) => [node.id, node])), adjacent = new Map(nodes.map((node) => [node.id, new Set()]));
    (full.edges || []).forEach((edge) => { if (adjacent.has(edge.source) && adjacent.has(edge.target)) { adjacent.get(edge.source).add(edge.target); adjacent.get(edge.target).add(edge.source); } });
    const matches = query ? nodes.filter((node) => [node.label, node.id, node.relationId, node.record?.id, node.record?.role, node.record?.title].some((value) => text(value).toLowerCase().includes(query))) : [];
    const seeds = focusId && byId.has(focusId) ? [focusId] : matches.map((node) => node.id), selected = new Set();
    if (seeds.length) seeds.forEach((nodeId) => { selected.add(nodeId); (adjacent.get(nodeId) || []).forEach((next) => selected.add(next)); }); else if (!query && !focusId) nodes.forEach((node) => selected.add(node.id));
    const seedSet = new Set(seeds), priority = [...seeds.map((nodeId) => byId.get(nodeId)).filter(Boolean), ...sort([...selected].filter((nodeId) => !seedSet.has(nodeId)).map((nodeId) => byId.get(nodeId)).filter(Boolean))], visibleIds = new Set(priority.slice(0, maxNodes).map((node) => node.id));
    const visibleNodes = priority.filter((node) => visibleIds.has(node.id)), visibleEdges = (full.edges || []).filter((edge) => visibleIds.has(edge.source) && visibleIds.has(edge.target));
    return { nodes: visibleNodes, edges: visibleEdges, meta: { totalNodes: nodes.length, totalEdges: (full.edges || []).length, loadedRecords: nodes.filter((node) => !node.missing).length, missingReferences: nodes.filter((node) => node.missing).length, filteredNodes: selected.size, filteredEdges: (full.edges || []).filter((edge) => selected.has(edge.source) && selected.has(edge.target)).length, visibleNodes: visibleNodes.length, visibleEdges: visibleEdges.length, truncated: selected.size > visibleNodes.length, hiddenNodes: Math.max(0, selected.size - visibleNodes.length), query, matchedNodeIds: matches.map((node) => node.id), scopeKey: text(options.scopeKey), focusId } };
  }

  // Aggregates only project recorded nodes and edges. `memberToDisplay` maps canonical ID to display ID.
  // `expandedGroups` accepts a Set or array of stable group IDs. Every display edge has canonicalEdgeIds.
  function projectGraph(graph, options = {}) {
    const canonical = filterGraph(graph, { ...options, maxNodes: Number.isFinite(options.maxCanonicalNodes) ? options.maxCanonicalNodes : 5000 });
    const includeLineage = Boolean(options.includeLineage), groupHistorical = options.groupHistorical !== false, expanded = new Set(options.expandedGroups || []), matches = new Set(canonical.meta.matchedNodeIds), byId = new Map(canonical.nodes.map((node) => [node.id, node]));
    const edges = canonical.edges.filter((edge) => includeLineage || edge.type !== "parent-lineage"), ownership = new Map();
    edges.forEach((edge) => { if ((edge.type === "job-agent" || edge.type === "goal-agent") && byId.get(edge.target)?.type === "agent") { const list = ownership.get(edge.target) || []; list.push(edge); ownership.set(edge.target, list); } });
    const proposed = new Map();
    canonical.nodes.forEach((node) => {
      const record = node.record || {}, state = text(record.state || record.status || record.outcome).toLowerCase();
      if (!groupHistorical || node.type !== "agent" || !TERMINAL.has(state) || matches.has(node.id) || text(options.focusId) === node.id) return;
      const owners = (ownership.get(node.id) || []).sort((a, b) => a.source.localeCompare(b.source));
      const owner = owners[0];
      // A group represents a recorded membership edge. Orphans remain individually inspectable,
      // and a visible lineage endpoint remains individual when lineage is requested.
      if (owners.length !== 1 || !owner || canonical.edges.some((edge) => edge.type === "parent-lineage" && (edge.source === node.id || (includeLineage && edge.target === node.id)))) return;
      const anchorId = owner.source, role = text(record.role || node.label || "Unspecified"), currentId = groupId(anchorId, role);
      if (expanded.has(currentId)) return; const group = proposed.get(currentId) || { id: currentId, anchorId, role, memberIds: [] }; group.memberIds.push(node.id); proposed.set(currentId, group);
    });
    const folded = new Map(); proposed.forEach((group) => { if (group.memberIds.length > 1) group.memberIds.forEach((member) => folded.set(member, group)); });
    const memberToDisplay = Object.create(null), displayNodes = [];
    canonical.nodes.forEach((node) => { const group = folded.get(node.id); if (group) { memberToDisplay[node.id] = group.id; return; } memberToDisplay[node.id] = node.id; displayNodes.push({ ...node, memberIds: [node.id], groupedCount: 1 }); });
    proposed.forEach((group) => { if (group.memberIds.length > 1) displayNodes.push({ id: group.id, type: "agent-group", label: `${group.role} · ${number(group.memberIds.length)} historical runs`, relationId: group.anchorId === "unassigned" ? "No recorded goal or job" : group.anchorId, anchorId: group.anchorId, memberIds: [...group.memberIds], groupedCount: group.memberIds.length, missing: false, record: null }); });
    const displayById = new Map(displayNodes.map((node) => [node.id, node])), displayEdges = new Map();
    edges.forEach((edge) => { const source = memberToDisplay[edge.source], target = memberToDisplay[edge.target]; if (!source || !target || (source === target && (edge.source !== edge.target || source !== edge.source)) || !displayById.has(source) || !displayById.has(target)) return; const edgeId = `display:${source}->${target}:${edge.type}`, current = displayEdges.get(edgeId) || { id: edgeId, source, target, type: edge.type, canonicalEdgeIds: [], count: 0 }; current.canonicalEdgeIds.push(edge.id); current.count += 1; displayEdges.set(edgeId, current); });
    const canonicalPriority = new Map(canonical.nodes.map((node, index) => [node.id, index]));
    const displayPriority = (node) => Math.min(...(node.memberIds || [node.id]).map((member) => canonicalPriority.get(member) ?? Number.MAX_SAFE_INTEGER));
    const maxNodes = Number.isFinite(options.maxNodes) ? Math.max(1, Math.floor(options.maxNodes)) : MAX_VISIBLE_NODES, visible = [...displayNodes].sort((left, right) => displayPriority(left) - displayPriority(right) || text(left.id).localeCompare(text(right.id))), ids = new Set(visible.slice(0, maxNodes).map((node) => node.id)), nodes = visible.filter((node) => ids.has(node.id)), resultEdges = [...displayEdges.values()].filter((edge) => ids.has(edge.source) && ids.has(edge.target)).sort((a, b) => a.id.localeCompare(b.id)), groups = displayNodes.filter((node) => node.type === "agent-group");
    const hiddenDisplayNodes = Math.max(0, displayNodes.length - nodes.length);
    return { nodes, edges: resultEdges, memberToDisplay, canonical, meta: { ...canonical.meta, truncated: canonical.meta.truncated || hiddenDisplayNodes > 0, displayedNodes: nodes.length, displayedEdges: resultEdges.length, hiddenDisplayNodes, groupedAgents: groups.reduce((total, group) => total + group.groupedCount, 0), groupCount: groups.length, includeLineage, expandedGroups: [...expanded] } };
  }

  // Compact, truthful landing view. Job and execution summaries only remap existing
  // canonical endpoints; `canonicalEdgeIds` remains the evidence for every line.
  function projectOverview(graph, options = {}) {
    const canonical = filterGraph(graph, { maxNodes: 5000 });
    const byId = new Map(canonical.nodes.map((node) => [node.id, node]));
    const jobGoal = new Map(), memberToDisplay = Object.create(null), nodes = [];
    canonical.nodes.filter((node) => node.type === "goal").forEach((node) => { nodes.push({ ...node, memberIds: [node.id], groupedCount: 1 }); memberToDisplay[node.id] = node.id; });
    canonical.nodes.filter((node) => node.type === "job").forEach((node) => {
      const goalEdge = canonical.edges.find((edge) => edge.type === "goal-job" && edge.target === node.id), bucket = goalEdge ? goalEdge.source : "unassigned";
      jobGoal.set(node.id, bucket); memberToDisplay[node.id] = `job-group:${bucket}`;
    });
    const jobs = new Map(); canonical.nodes.filter((node) => node.type === "job").forEach((node) => { const key = memberToDisplay[node.id], group = jobs.get(key) || { id: key, type: "job-group", label: "", memberIds: [], groupedCount: 0, missing: false }; group.memberIds.push(node.id); group.groupedCount += 1; jobs.set(key, group); });
    jobs.forEach((group) => { const owner = group.id.slice(10), goal = byId.get(owner); group.label = `${number(group.groupedCount)} recorded job${group.groupedCount === 1 ? "" : "s"}`; group.summary = true; nodes.push(group); });
    const memberships = new Map(); canonical.edges.filter((edge) => edge.type === "job-agent" || edge.type === "goal-agent").forEach((edge) => { const list = memberships.get(edge.target) || []; list.push(edge); memberships.set(edge.target, list); });
    const agents = new Map(); canonical.nodes.filter((node) => node.type === "agent").forEach((node) => {
      const links = memberships.get(node.id) || [], state = text(node.record?.state || node.record?.status || node.record?.outcome).toLowerCase(), stateBucket = TERMINAL.has(state) ? "terminal" : "active";
      if (links.length !== 1 || !TERMINAL.has(state)) { memberToDisplay[node.id] = node.id; nodes.push({ ...node, memberIds: [node.id], groupedCount: 1 }); return; }
      const link = links[0], owner = link.type === "job-agent" ? (memberToDisplay[link.source] || link.source) : link.source, key = `agent-overview:${owner}:${stateBucket}`;
      memberToDisplay[node.id] = key; const group = agents.get(key) || { id: key, type: "agent-group", label: "", memberIds: [], groupedCount: 0, summary: true, missing: false, ownerIds: new Set() }; group.memberIds.push(node.id); group.groupedCount += 1; group.ownerIds.add(owner); agents.set(key, group);
    });
    agents.forEach((group) => { group.label = `${number(group.groupedCount)} historical runs`; group.ownerIds = [...group.ownerIds]; nodes.push(group); });
    const edges = new Map(); canonical.edges.filter((edge) => options.includeLineage || edge.type !== "parent-lineage").forEach((edge) => { const source = memberToDisplay[edge.source] || edge.source, target = memberToDisplay[edge.target] || edge.target; if (!source || !target || (source === target && (edge.source !== edge.target || source !== edge.source))) return; const key = `overview:${source}->${target}:${edge.type}`, result = edges.get(key) || { id: key, source, target, type: edge.type, canonicalEdgeIds: [], count: 0 }; result.canonicalEdgeIds.push(edge.id); result.count += 1; edges.set(key, result); });
    canonical.nodes.filter((node) => node.missing).forEach((node) => { memberToDisplay[node.id] = node.id; nodes.push({ ...node, memberIds: [node.id], groupedCount: 1 }); });
    const maxNodes = Number.isFinite(options.maxNodes) ? Math.max(1, Math.floor(options.maxNodes)) : 35, shown = nodes.slice(0, maxNodes), ids = new Set(shown.map((node) => node.id)), hiddenDisplayNodes = Math.max(0, nodes.length - shown.length);
    return { nodes: shown, edges: [...edges.values()].filter((edge) => ids.has(edge.source) && ids.has(edge.target)), memberToDisplay, canonical, meta: { totalNodes: canonical.meta.totalNodes, totalEdges: canonical.meta.totalEdges, loadedRecords: canonical.meta.loadedRecords, missingReferences: canonical.meta.missingReferences, groupedAgents: [...agents.values()].reduce((total, group) => total + group.groupedCount, 0), groupCount: agents.size + jobs.size, overview: true, hiddenDisplayNodes, truncated: hiddenDisplayNodes > 0 } };
  }


  function projectGroupPage(graph, group, options = {}) {
    const overview = projectOverview(graph, { maxNodes: 5000 });
    const byId = new Map(graph.nodes.map(node => [node.id, node]));
    const displayed = new Map(overview.nodes.map(node => [node.id, node]));
    const members = (group.memberIds || []).filter(id => byId.has(id));
    const pageSize = Math.min(12, Math.max(1, Math.floor(options.pageSize || 8)));
    const pages = Math.max(1, Math.ceil(members.length / pageSize));
    const page = Math.min(pages - 1, Math.max(0, Math.floor(options.page || 0)));
    const pageIds = members.slice(page * pageSize, (page + 1) * pageSize);
    const pageSet = new Set(pageIds), nodes = new Map(), edges = new Map();
    pageIds.forEach(id => nodes.set(id, { ...byId.get(id), memberIds: [id], groupedCount: 1 }));
    for (const edge of graph.edges) {
      if (!pageSet.has(edge.target) || (edge.type === "parent-lineage" && !options.includeLineage)) continue;
      const source = pageSet.has(edge.source) || edge.type === "parent-lineage"
        ? edge.source : (overview.memberToDisplay[edge.source] || edge.source);
      const owner = displayed.get(source) || byId.get(source);
      if (!owner) continue;
      if (!nodes.has(source)) nodes.set(source, owner);
      const key = `page:${source}->${edge.target}:${edge.type}`;
      const item = edges.get(key) || { id: key, source, target: edge.target, type: edge.type, canonicalEdgeIds: [], count: 0 };
      item.canonicalEdgeIds.push(edge.id); item.count++; edges.set(key, item);
    }
    return {
      nodes: [...nodes.values()], edges: [...edges.values()], canonical: overview.canonical,
      meta: { ...overview.meta, overview: false, groupCount: 0, groupedAgents: 0,
        hiddenDisplayNodes: 0, truncated: false,
        paging: { page, pages, pageSize, total: members.length, start: members.length ? page * pageSize + 1 : 0, end: Math.min((page + 1) * pageSize, members.length), groupId: group.id, label: group.label } }
    };
  }

  function create(element, options = {}) {
    if (!element || !element.ownerDocument) throw new Error("A map host element is required.");
    const doc = element.ownerDocument, win = typeof window !== "undefined" ? window : globalThis, make = (tag, className, value) => { const node = doc.createElement(tag); if (className) node.className = className; if (value != null) node.textContent = value; return node; };
    const root = make("div", "relationship-map"), toolbar = make("div", "relationship-map-toolbar"), searchLabel = make("label", "relationship-map-search"), search = make("input"), pickerLabel = make("label", "relationship-map-picker"), picker = make("select"), presetLabel = make("label", "relationship-map-picker"), preset = make("select"), resetLayout = make("button", "text-button", "Reset layout"), focus = make("button", "text-button", "Focus selected"), readable = make("button", "text-button relationship-map-readable", "Readable view"), toggle = make("label", "relationship-map-toggle"), lineage = make("input"), zoomOut = make("button", "icon-button", "−"), zoomIn = make("button", "icon-button", "+"), fit = make("button", "text-button", "Fit all");
    searchLabel.htmlFor = "relationship-map-search-input"; searchLabel.append(make("span", "sr-only", "Search the relationship map")); search.type = "search"; search.id = "relationship-map-search-input"; search.placeholder = "Search goals, jobs, agents, or references"; search.autocomplete = "off"; searchLabel.append(search);
    pickerLabel.append(make("span", "sr-only", "Jump to a map record or reference")); picker.id = "relationship-map-node-picker"; picker.setAttribute("aria-label", "Jump to a map record or reference"); pickerLabel.append(picker);
    presetLabel.append(make("span", "sr-only", "Map preset")); preset.setAttribute("aria-label", "Map preset"); [["all", "All recorded"], ["active", "Active last reported state"], ["failed", "Failed last reported state"]].forEach(([value, label]) => { const option = make("option", "", label); option.value = value; preset.append(option); }); presetLabel.append(preset); resetLayout.type = "button"; resetLayout.setAttribute("aria-label", "Reset layout");
    focus.type = "button"; focus.disabled = true; focus.setAttribute("aria-pressed", "false"); readable.type = "button"; readable.setAttribute("aria-label", "Return to a readable map scale"); lineage.type = "checkbox"; toggle.append(lineage, make("span", "", "Parent lineage")); zoomOut.type = "button"; zoomOut.setAttribute("aria-label", "Zoom out"); zoomIn.type = "button"; zoomIn.setAttribute("aria-label", "Zoom in"); fit.type = "button"; toolbar.append(searchLabel, pickerLabel, presetLabel, resetLayout, focus, readable, toggle, zoomOut, zoomIn, fit);
    const summary = make("p", "relationship-map-summary"); summary.setAttribute("role", "status"); summary.setAttribute("aria-live", "polite");
    const guidance = make("p", "relationship-map-guidance", "Readable view keeps node labels at a comfortable size; pan with the arrow keys or drag the canvas. Fit all shows every record.");
    const legend = make("ul", "relationship-map-legend"); [["goal", "Goal"], ["job", "Job"], ["job-group", "Job group"], ["agent", "Individual run"], ["agent-group", "Historical run group"], ["missing", "Not-loaded reference"], ["goal-job", "Goal → job"], ["job-agent", "Job → run"], ["direct", "Goal membership"], ["lineage", "Parent lineage"]].forEach(([kind, label]) => { const item = make("li", `relationship-map-legend-${kind}`); item.append(make("span"), make("span", "", label)); legend.append(item); });
    const workspace = make("div", "relationship-map-workspace"), wrap = make("div", "relationship-map-canvas"), canvas = make("div", "relationship-map-cytoscape"), details = make("aside", "relationship-map-details"); canvas.tabIndex = 0; canvas.setAttribute("role", "application"); canvas.setAttribute("aria-label", "Interactive recorded relationship map. Use search and the record picker to explore without a pointer."); details.hidden = true; details.setAttribute("aria-live", "polite"); wrap.append(canvas); workspace.append(wrap, details); const navigation = make("nav", "relationship-map-navigation"); navigation.setAttribute("aria-label", "Map scope");
    const home = make("button", "text-button", "Overview"), breadcrumb = make("span", "relationship-map-breadcrumb", "All recorded relationships"), previous = make("button", "text-button", "Previous page"), nextPage = make("button", "text-button", "Next page");
    breadcrumb.setAttribute("role", "status"); breadcrumb.setAttribute("aria-live", "polite");
    home.type = previous.type = nextPage.type = "button"; navigation.append(home, breadcrumb, previous, nextPage);
    root.append(toolbar, navigation, summary, guidance, legend, workspace); element.replaceChildren(root);
    if (!win.cytoscape) { summary.textContent = "The local relationship map renderer did not load. Refresh this page to retry."; return { update() {}, destroy() { element.replaceChildren(); } }; }
    if (win.cytoscapeFcose && !win.__tasktraFcoseRegistered) { win.cytoscape.use(win.cytoscapeFcose); win.__tasktraFcoseRegistered = true; }
    const cy = win.cytoscape({ container: canvas, elements: [], wheelSensitivity: .17, style: [
      { selector: "node", style: { label: "data(label)", color: "#f4f8ff", "font-size": 17, "font-weight": 600, "text-wrap": "wrap", "text-max-width": "data(textMaxWidth)", "text-valign": "center", "text-halign": "center", "background-color": "#213c59", "border-width": 2.5, "border-color": "#76b8ff", width: "data(width)", height: "data(height)", "overlay-opacity": 0, "text-outline-width": 0, "text-outline-color": "#10213a" } },
      { selector: 'node[type = "goal"]', style: { shape: "round-rectangle", "background-color": "#573122", "border-color": "#ff9b75" } }, { selector: 'node[type = "job"], node[type = "job-group"]', style: { shape: "round-rectangle", "background-color": "#183b5b", "border-color": "#77baff" } }, { selector: 'node[type = "agent"]', style: { shape: "round-rectangle", "background-color": "#17463f", "border-color": "#77e0c1" } }, { selector: 'node[type = "agent-group"]', style: { shape: "round-rectangle", "background-color": "#16443d", "border-color": "#77e0c1", "border-style": "dashed" } }, { selector: 'node[missing = "yes"]', style: { shape: "round-rectangle", "background-color": "#293244", "border-color": "#a8afba", "border-style": "dashed" } },
      { selector: "edge", style: { width: 2.2, "curve-style": "bezier", "line-color": "#61809e", opacity: .78, "overlay-opacity": 0 } }, { selector: 'edge[type = "goal-job"]', style: { "line-color": "#ff9b75" } }, { selector: 'edge[type = "job-agent"]', style: { "line-color": "#77e0c1" } }, { selector: 'edge[type = "goal-agent"]', style: { "line-color": "#b7a9ff", "line-style": "dotted" } }, { selector: 'edge[type = "parent-lineage"]', style: { "line-color": "#c4a9ff", "line-style": "dashed", "target-arrow-shape": "triangle", "target-arrow-color": "#c4a9ff" } }, { selector: ".is-selected", style: { "border-color": "#fff", "border-width": 4, "shadow-blur": 14, "shadow-color": "#b9fff0", "shadow-opacity": .65 } }, { selector: ".is-muted", style: { opacity: .14 } }
    ] });
    canvas._tasktraCy = cy;
    let detailSignature = "", pickerSignature = "";
    let graph = { nodes: [], edges: [] }, view = null, selected = "", focusId = "", groups = new Set(), overview = true, groupScope = null, signature = "", active = false, timer = null, needsLayout = false, presetValue = "all", layoutIdentity = "", layoutOptions = { projectKey: "", scopeKey: "all", demo: false }, userPositions = new Map();
    const positions = new Map(), canonical = (nodeId) => graph.nodes.find((node) => node.id === nodeId), projected = (nodeId) => view && view.nodes.find((node) => node.id === nodeId), graphSignature = (result) => JSON.stringify([result.nodes.map((node) => [node.id, node.label, node.type, nodePresentation(node).label]), result.edges.map((edge) => edge.id)]), activeGraph = () => presetGraph(graph, presetValue);
    const readPositions = () => { try { const raw = win.localStorage?.getItem(layoutIdentity); if (!raw || raw.length > 24000) return new Map(); return validPositions(JSON.parse(raw), new Set(graph.nodes.map((node) => node.id))); } catch (_) { return new Map(); } };
    const savePositions = () => { try { if (!layoutIdentity || !userPositions.size) return; win.localStorage?.setItem(layoutIdentity, JSON.stringify({ version: 1, positions: [...userPositions.entries()].slice(0, MAX_VISIBLE_NODES).map(([nodeId, point]) => [nodeId, point.x, point.y]) })); } catch (_) {} };
    const setLayoutIdentity = (nextOptions) => { layoutOptions = { ...layoutOptions, ...nextOptions }; const next = layoutKey(layoutOptions.projectKey, layoutOptions.scopeKey, presetValue, layoutOptions.demo); if (next === layoutIdentity) return false; layoutIdentity = next; positions.clear(); userPositions = readPositions(); userPositions.forEach((point, nodeId) => positions.set(nodeId, point)); signature = ""; return true; };
    const toNode = (node) => ({ group: "nodes", data: { id: node.id, ...nodePresentation(node), type: node.type, missing: node.missing ? "yes" : "" } }), toEdge = (edge) => ({ group: "edges", data: { id: edge.id, source: edge.source, target: edge.target, type: edge.type, count: edge.count, canonicalEdgeIds: edge.canonicalEdgeIds } });
    const selection = () => { const ids = new Set(selected ? [selected] : []); if (selected) cy.$id(selected).connectedEdges().forEach((edge) => { ids.add(edge.source().id()); ids.add(edge.target().id()); }); return ids; };
    const applySelection = () => { const show = cy.$id(selected).length > 0, near = selection(); cy.elements().removeClass("is-selected is-muted"); if (show) { cy.$id(selected).addClass("is-selected"); cy.nodes().forEach((node) => { if (!near.has(node.id())) node.addClass("is-muted"); }); cy.edges().forEach((edge) => { if (!near.has(edge.source().id()) || !near.has(edge.target().id())) edge.addClass("is-muted"); }); } const node = projected(selected); focus.disabled = !show || !canonical(selected); focus.textContent = "Focus selected"; focus.setAttribute("aria-pressed", String(Boolean(focusId))); picker.value = canonical(selected) ? selected : ""; };
    const field = (label, value) => { const row = make("div", "detail-item"); row.append(make("b", "", label), make("span", "", value || "Unavailable")); return row; };
    const closeDetails = () => { selected = ""; applySelection(); renderDetails(); canvas.focus({ preventScroll: true }); };
    const openGroup = node => {
      groupScope = { id: node.id, page: 0 };
      selected = ""; focusId = ""; overview = false; search.value = "";
      refresh({ layout: true, fit: true }); home.focus({ preventScroll: true });
    };
    const renderDetails = () => {
      const currentNode = projected(selected);
      const nextSignature = JSON.stringify([selected, currentNode, lineage.checked,
        graph.edges.filter(edge => edge.source === selected || edge.target === selected)
          .map(edge => [edge.id, canonical(edge.source)?.label, canonical(edge.target)?.label])]);
      if (nextSignature === detailSignature) return;
      detailSignature = nextSignature;
      const focused = details.contains(doc.activeElement) ? doc.activeElement?.dataset.mapAction : null;
      const node = projected(selected); details.replaceChildren(); details.hidden = !node;
      if (!node) return;
      const heading = make("div", "relationship-map-detail-heading");
      const close = make("button", "icon-button relationship-map-close", "\u00d7");
      close.type = "button"; close.setAttribute("aria-label", "Close node details"); close.dataset.mapAction = "close"; close.addEventListener("click", closeDetails);
      heading.append(make("h3", "", node.label), close); details.append(heading);
      if (node.summary || node.type === "agent-group" || node.type === "job-group") {
        details.append(make("p", "", `${number(node.groupedCount)} recorded members. Open this group to browse a readable page of exact records.`));
        const open = make("button", "button button-accent relationship-map-open", "Open group");
        open.type = "button"; open.dataset.mapAction = "open"; open.addEventListener("click", () => openGroup(projected(selected) || node)); details.append(open);
      } else {
        const raw = canonical(node.id), record = raw?.record || {};
        const relations = graph.edges.filter(edge => edge.source === node.id || edge.target === node.id)
          .filter(edge => lineage.checked || edge.type !== "parent-lineage")
          .map(edge => `${edge.type.replace(/-/g, " ")} \u00b7 ${(canonical(edge.source === node.id ? edge.target : edge.source) || {}).label || "not loaded"}`);
        details.append(make("p", "eyebrow", raw?.missing ? "Reference not loaded" : `${raw?.type || "record"} record`));
        const grid = make("div", "relationship-map-detail-grid");
        grid.append(field("Record ID", raw?.relationId || node.id), field("Known state", record.state || record.status || record.outcome || (raw?.missing ? "Not loaded" : "Unavailable")),
          field("Model / effort", [record.observed_model || record.model, record.observed_effort || record.effort].filter(Boolean).join(" \u00b7 ") || "Unavailable"),
          field("Tokens", Number.isFinite(record.total_tokens) ? number(record.total_tokens) : "Unavailable"), field("Explicit relations", relations.join("\n") || "None recorded"));
        details.append(grid);
        if (raw?.record && !raw.missing) {
          const open = make("button", "button button-accent relationship-map-open", "Open record"); open.type = "button"; open.dataset.mapAction = "open";
          open.addEventListener("click", () => { const latest = canonical(node.id); if (latest?.record) options.onOpen?.({ type: latest.type, record: latest.record }); }); details.append(open);
        }
      }
      if (focused) details.querySelector(`[data-map-action="${focused}"]`)?.focus({ preventScroll: true });
    };
    details.addEventListener("keydown", event => { if (event.key === "Escape") { event.preventDefault(); closeDetails(); } });
    // Preserve fCoSE's organic arrangement inside each disconnected component,
    // then pack the components to avoid wasting the viewport on empty space.
    const packComponents = () => {
      const components = cy.elements().components().map(eles => ({ eles, box: eles.nodes().boundingBox({ includeLabels: true, includeOverlays: false }) }))
        .sort((a, b) => b.box.h - a.box.h || b.box.w - a.box.w);
      if (components.length < 2) return;
      const gap = 28, aspect = Math.max(1, canvas.clientWidth) / Math.max(1, canvas.clientHeight);
      const area = components.reduce((sum, item) => sum + (item.box.w + gap) * (item.box.h + gap), 0);
      const widest = Math.max(...components.map(item => item.box.w));
      let best = null;
      for (const factor of [.65, .8, 1, 1.2, 1.4, 1.65, 2]) {
        const target = Math.max(widest, Math.sqrt(area * aspect) * factor);
        let x = 0, y = 0, row = 0, width = 0; const placements = [];
        for (const item of components) {
          if (x && x + item.box.w > target) { x = 0; y += row + gap; row = 0; }
          placements.push({ item, x, y }); width = Math.max(width, x + item.box.w);
          x += item.box.w + gap; row = Math.max(row, item.box.h);
        }
        const height = y + row, score = Math.min((canvas.clientWidth - 60) / width, (canvas.clientHeight - 60) / height);
        if (!best || score > best.score) best = { score, placements };
      }
      cy.batch(() => best.placements.forEach(({ item, x, y }) => {
        const dx = x - item.box.x1, dy = y - item.box.y1;
        item.eles.nodes().positions(node => ({ x: node.position("x") + dx, y: node.position("y") + dy }));
      }));
    };
    // fCoSE includes label dimensions, but a compact connected layout can still leave
    // two padded labels touching. Resolve only those fresh-layout collisions before
    // restoring any user-dragged positions.
    const separateOverlaps = () => {
      const gap = 26;
      for (let pass = 0; pass < 12; pass++) {
        let moved = false;
        const nodes = cy.nodes().toArray();
        for (let index = 0; index < nodes.length; index++) for (let next = index + 1; next < nodes.length; next++) {
          const left = nodes[index], right = nodes[next], a = left.boundingBox({ includeLabels: true, includeOverlays: false }), b = right.boundingBox({ includeLabels: true, includeOverlays: false });
          const overlapX = Math.min(a.x2, b.x2) - Math.max(a.x1, b.x1), overlapY = Math.min(a.y2, b.y2) - Math.max(a.y1, b.y1);
          if (overlapX <= 0 || overlapY <= 0) continue;
          const leftCenter = left.position(), rightCenter = right.position(), horizontal = overlapX <= overlapY;
          const sign = horizontal ? (rightCenter.x >= leftCenter.x ? 1 : -1) : (rightCenter.y >= leftCenter.y ? 1 : -1), amount = (horizontal ? overlapX : overlapY) / 2 + gap / 2;
          if (horizontal) { left.position({ x: leftCenter.x - sign * amount, y: leftCenter.y }); right.position({ x: rightCenter.x + sign * amount, y: rightCenter.y }); }
          else { left.position({ x: leftCenter.x, y: leftCenter.y - sign * amount }); right.position({ x: rightCenter.x, y: rightCenter.y + sign * amount }); }
          moved = true;
        }
        if (!moved) return;
      }
    };
    const readableView = (elements = cy.elements()) => {
      if (!elements.length) return;
      cy.fit(elements, 42);
      if (cy.zoom() < READABLE_MIN_ZOOM) {
        cy.zoom({ level: READABLE_MIN_ZOOM, renderedPosition: { x: canvas.clientWidth / 2, y: canvas.clientHeight / 2 } });
        const target = selected ? cy.$id(selected) : cy.collection();
        cy.center(target.length ? target : elements);
      }
    };
    const layout = (fitView) => { if (!cy.nodes().length) { canvas.dataset.mapReady = "true"; needsLayout = false; return; } if (!active) { needsLayout = true; return; } needsLayout = false; if (timer) win.clearTimeout(timer); canvas.dataset.mapReady = "false"; const runner = cy.layout({ name: "fcose", quality: "default", randomize: true, animate: false, fit: false, nodeDimensionsIncludeLabels: true, padding: 40, nodeRepulsion: 4800, idealEdgeLength: 150, edgeElasticity: .12, gravity: .28, numIter: 2600, tile: true, tilingPaddingVertical: 30, tilingPaddingHorizontal: 30 }); cy.one("layoutstop", () => { packComponents(); separateOverlaps(); userPositions.forEach((point, nodeId) => { const node = cy.$id(nodeId); if (node.length) node.position(point); }); cy.nodes().forEach((node) => positions.set(node.id(), node.position())); if (fitView) readableView(); canvas.dataset.mapReady = "true"; }); runner.run(); };
    const syncPicker = () => { const next = JSON.stringify(activeGraph().nodes.map(node => [node.id, node.label, node.relationId])); if (next === pickerSignature) return; pickerSignature = next; const current = picker.value, placeholder = make("option", "", "Jump to a map record or reference"); placeholder.value = ""; picker.replaceChildren(placeholder); sort(activeGraph().nodes).forEach((node) => { const option = make("option", "", `${node.type.replace(/-/g, " ")} · ${node.label} · ${node.relationId || node.id}`); option.value = node.id; picker.append(option); }); picker.value = [...picker.options].some((option) => option.value === current) ? current : ""; };
    const refresh = ({ layout: shouldLayout = false, fit: fitView = false } = {}) => {
      const source = activeGraph();
      if (groupScope) {
        const latest = projectOverview(source, { maxNodes: 5000 }).nodes.find(node => node.id === groupScope.id)
          || projectGraph(source, { maxNodes: 5000 }).nodes.find(node => node.id === groupScope.id);
        if (latest) {
          view = projectGroupPage(source, latest, { page: groupScope.page, includeLineage: lineage.checked });
          groupScope.page = view.meta.paging.page;
        } else {
          groupScope = null; selected = ""; focusId = ""; overview = true;
          view = projectOverview(source, { includeLineage: lineage.checked });
          shouldLayout = true; fitView = true;
        }
      } else {
        view = overview && !search.value.trim() && !focusId ? projectOverview(source, { includeLineage: lineage.checked })
          : projectGraph(source, { query: search.value, focusId, includeLineage: lineage.checked, expandedGroups: groups, maxNodes: MAX_VISIBLE_NODES });
      }
      if (selected && canonical(selected) && !view.nodes.some(node => node.id === selected)) {
        // A selected run remains inspectable when a poll moves it into a summary.
        groupScope = null; overview = false; focusId = selected;
        view = projectGraph(source, { focusId, includeLineage: lineage.checked });
      }
      const next = graphSignature(view), changed = next !== signature;
      const projectedTopologyChanged = changed && (view.nodes.some(node => !cy.$id(node.id).length)
        || cy.nodes().some(node => !view.nodes.some(item => item.id === node.id))
        || view.edges.some(edge => !cy.$id(edge.id).length)
        || cy.edges().some(edge => !view.edges.some(item => item.id === edge.id)));
      shouldLayout = shouldLayout || projectedTopologyChanged;
      if (changed) {
        cy.nodes().forEach(node => positions.set(node.id(), node.position())); cy.elements().remove();
        cy.add([...view.nodes.map(toNode), ...view.edges.map(toEdge)]);
        cy.nodes().forEach(node => { if (positions.has(node.id())) node.position(positions.get(node.id())); }); signature = next;
      }
      if (!view.nodes.some(node => node.id === selected)) selected = "";
      const meta = view.meta, paging = meta.paging;
      previous.hidden = nextPage.hidden = !paging;
      previous.disabled = !paging || paging.page === 0; nextPage.disabled = !paging || paging.page + 1 >= paging.pages;
      const presetText = presetValue === "all" ? "All recorded relationships" : presetValue === "active" ? "Active last reported state" : "Failed last reported state";
      const scopeText = paging ? `${paging.label} \u00b7 ${paging.start}\u2013${paging.end} of ${paging.total} \u00b7 Page ${paging.page + 1} of ${paging.pages}`
        : search.value.trim() ? `Search: ${search.value.trim()}` : focusId ? `Connections: ${canonical(focusId)?.label || focusId}` : presetText;
      if (breadcrumb.textContent !== scopeText) breadcrumb.textContent = scopeText;
      const groupsText = meta.overview ? ` Overview: ${number(view.nodes.length)} nodes, including ${number(meta.groupedAgents)} historical runs in summaries.` : ` ${number(view.nodes.length)} nodes in this view.`;
      const limit = meta.hiddenDisplayNodes || meta.hiddenNodes || 0;
      const summaryText = `${number(meta.loadedRecords)} loaded records, ${number(meta.missingReferences)} not-loaded references, and ${number(meta.totalEdges)} explicit connections.${groupsText}${limit ? ` ${number(limit)} nodes are outside the display limit; search or use the record picker to find them.` : ""}${search.value.trim() && !meta.matchedNodeIds?.length ? " No records match this search." : ""}${presetValue !== "all" && !view.nodes.length ? " No records match this preset." : ""}`;
      if (summary.textContent !== summaryText) summary.textContent = summaryText;
      applySelection(); renderDetails();
      if (shouldLayout || (changed && !positions.size)) layout(fitView || !positions.size);
      else if (fitView && active) readableView();
    };
    const select = (nodeId, makeFocus) => {
      selected = nodeId;
      if (makeFocus && canonical(nodeId)) {
        groupScope = null; focusId = nodeId; overview = false;
        refresh({ layout: true, fit: true });
      } else { applySelection(); renderDetails(); }
    };
    const showOverview = () => {
      groupScope = null; focusId = ""; selected = ""; overview = true; search.value = ""; groups.clear();
      refresh({ layout: true, fit: true });
    };
    home.addEventListener("click", showOverview);
    previous.addEventListener("click", () => { if (groupScope && groupScope.page > 0) { groupScope.page--; selected = ""; refresh({ layout: true, fit: true }); } });
    nextPage.addEventListener("click", () => { if (groupScope && !nextPage.disabled) { groupScope.page++; selected = ""; refresh({ layout: true, fit: true }); } });
    search.addEventListener("input", () => { groupScope = null; selected = ""; focusId = ""; overview = true; refresh({ layout: true, fit: true }); });
    picker.addEventListener("change", () => { if (picker.value) { search.value = ""; select(picker.value, true); } });
    focus.addEventListener("click", () => { if (selected && canonical(selected)) { search.value = ""; select(selected, true); } });
    lineage.addEventListener("change", () => refresh({ layout: true, fit: true }));
    preset.addEventListener("change", () => { presetValue = preset.value; setLayoutIdentity({}); groupScope = null; selected = ""; focusId = ""; overview = true; search.value = ""; syncPicker(); refresh({ layout: true, fit: true }); });
    resetLayout.addEventListener("click", () => { try { if (layoutIdentity) win.localStorage?.removeItem(layoutIdentity); } catch (_) {} userPositions.clear(); positions.clear(); signature = ""; refresh({ layout: true, fit: true }); });
    readable.addEventListener("click", () => readableView());
    zoomIn.addEventListener("click", () => cy.zoom({ level: Math.min(3, cy.zoom() * 1.22), renderedPosition: { x: canvas.clientWidth / 2, y: canvas.clientHeight / 2 } }));
    zoomOut.addEventListener("click", () => cy.zoom({ level: Math.max(.16, cy.zoom() / 1.22), renderedPosition: { x: canvas.clientWidth / 2, y: canvas.clientHeight / 2 } }));
    fit.addEventListener("click", () => cy.fit(cy.elements(), 30));
    cy.on("tap", "node", event => select(event.target.id(), false));
    cy.on("dragfree", "node", event => { if (!canonical(event.target.id())) return; const point = event.target.position(); if (!Number.isFinite(point.x) || !Number.isFinite(point.y)) return; userPositions.set(event.target.id(), { x: point.x, y: point.y }); savePositions(); });
    canvas.addEventListener("keydown", event => {
      if (event.key === "Escape") { event.preventDefault(); closeDetails(); return; }
      const pan = { ArrowLeft: [72, 0], ArrowRight: [-72, 0], ArrowUp: [0, 72], ArrowDown: [0, -72] }[event.key];
      if (pan) { event.preventDefault(); cy.panBy({ x: pan[0], y: pan[1] }); }
    });
    const resize = () => { if (active) cy.resize(); }, observer = typeof win.ResizeObserver === "function" ? new win.ResizeObserver(resize) : null; if (observer) observer.observe(wrap);
    return { update(snapshot, options = {}) { const old = JSON.stringify([graph.nodes.map((node) => node.id), graph.edges.map((edge) => edge.id)]), wasActive = active, pendingLayout = needsLayout; graph = buildGraph(snapshot); const identityChanged = setLayoutIdentity(options); const next = JSON.stringify([graph.nodes.map((node) => node.id), graph.edges.map((edge) => edge.id)]); active = Boolean(options.active); if (options.reset) { groupScope = null; overview = true; selected = ""; focusId = ""; groups = new Set(); search.value = ""; lineage.checked = false; positions.clear(); userPositions.forEach((point, nodeId) => positions.set(nodeId, point)); signature = ""; } syncPicker(); const shouldLayout = Boolean(options.reset) || identityChanged || old !== next || pendingLayout; refresh({ layout: shouldLayout, fit: Boolean(options.reset) || identityChanged || !signature || pendingLayout || !wasActive }); if (active) win.requestAnimationFrame(resize); }, setActive(value) { active = Boolean(value); if (active) win.requestAnimationFrame(() => { resize(); if (needsLayout) layout(true); }); }, getPreset() { return presetValue; }, setPreset(value) { const next = value === "active" || value === "failed" ? value : "all"; if (next === presetValue) return; presetValue = next; preset.value = next; setLayoutIdentity({}); groupScope = null; selected = ""; focusId = ""; overview = true; search.value = ""; syncPicker(); refresh({ layout: true, fit: true }); }, destroy() { if (timer) win.clearTimeout(timer); if (observer) observer.disconnect(); cy.destroy(); element.replaceChildren(); } };
  }
  return { buildGraph, filterGraph, projectGraph, projectOverview, projectGroupPage, presetGraph, runLabel, wrapNodeLabel, nodePresentation, READABLE_MIN_ZOOM, layoutKey, validPositions, create };
});
