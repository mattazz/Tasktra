(function(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.TasktraPortalInsights = api;
})(typeof window !== "undefined" ? window : globalThis, function() {
  "use strict";
  const MAX_ITEMS = 200;
  const SEVERITIES = new Set(["error", "warning", "info"]);
  const TARGETS = new Set(["goal", "job", "agent", "diagnostics"]);
  const text = (value, fallback = "Unavailable") => value == null || value === "" ? fallback : String(value);
  const number = (value) => Number.isFinite(value) ? new Intl.NumberFormat().format(value) : "Unavailable";
  const title = (value) => text(value, "unknown").replace(/[-_]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
  const time = (value) => {
    if (!value) return "Not recorded";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "Not recorded" : date.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  };
  function item(raw, index) {
    const input = raw && typeof raw === "object" ? raw : {};
    const target = input.target && typeof input.target === "object" ? input.target : {};
    return {
      id: text(input.id, `attention-${index}`), severity: SEVERITIES.has(input.severity) ? input.severity : "info",
      category: text(input.category, "recorded"), title: text(input.title, "Recorded attention item"), detail: text(input.detail, "No additional recorded detail."),
      goal_id: input.goal_id == null ? null : String(input.goal_id), job_id: input.job_id == null ? null : String(input.job_id), work_id: input.work_id == null ? null : String(input.work_id),
      occurred_at: input.occurred_at == null ? null : String(input.occurred_at),
      target: { type: TARGETS.has(target.type) ? target.type : "diagnostics", id: target.id == null ? null : String(target.id) },
    };
  }
  function synthetic() {
    return { available: true, demo: true, attention: { total: 2, shown: 2, partial: false, items: [
      item({ id: "demo-blocked", severity: "warning", category: "blocked job", title: "Demo job needs a decision", detail: "Representative browser-only attention item.", target: { type: "diagnostics", id: null } }, 0),
      item({ id: "demo-coverage", severity: "info", category: "coverage", title: "Demo usage is incomplete", detail: "Representative coverage notice; no live project diagnostic is shown.", target: { type: "diagnostics", id: null } }, 1),
    ] }, diagnostics: { build: { version: "demo", commit: null }, runtime: { available: true, schema_version: 1, expected_schema_version: 1, emergency_stopped: false }, coverage: { goal_id: null, loaded_agents: 3, measured_agents: 2, unknown_usage_agents: 1, missing_model_agents: 0, missing_job_agents: 0, partial: true }, telemetry: { last_observed_at: null }, validation: { available: false, report_id: null, status: null, started_at: null, finished_at: null, checks: [], partial: true } } };
  }
  function normalize(raw, options = {}) {
    if (options.demo) return synthetic();
    const object = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
    const input = object(raw) ? raw : null;
    const valid = input && input.version === 1 && object(input.attention) && Array.isArray(input.attention.items)
      && object(input.diagnostics) && ["build", "runtime", "coverage", "telemetry", "validation"].every((key) => object(input.diagnostics[key]));
    if (!valid) return { available: false, demo: false, attention: { items: [], total: 0, shown: 0, partial: true }, diagnostics: null };
    const attention = input.attention, diagnostics = input.diagnostics;
    const items = Array.isArray(attention.items) ? attention.items.slice(0, MAX_ITEMS).map(item) : [];
    return { available: true, demo: false, attention: { items, total: Number.isFinite(attention.total) ? attention.total : items.length, shown: Number.isFinite(attention.shown) ? attention.shown : items.length, partial: Boolean(attention.partial) }, diagnostics };
  }
  function attentionContext(entry, snapshot) {
    const kind = entry.target.type, id = entry.target.id;
    if (!id || kind === "diagnostics") return "Project diagnostics";
    const records = snapshot?.[kind === "goal" ? "goals" : kind === "job" ? "jobs" : "agents"] || [];
    const record = records.find((value) => (kind === "agent" ? value.work_id || value.id : value.id) === id);
    const name = record?.title || record?.role;
    const identity = id.length > 28 ? `${id.slice(0, 12)}…${id.slice(-10)}` : id;
    return `${name ? `${name} · ` : ""}${kind} ${identity}`;
  }
  function filterAttention(items, filters = {}) {
    return (items || []).filter((entry) => (!filters.severity || entry.severity === filters.severity) && (!filters.category || entry.category === filters.category));
  }
  function attentionSignature(data, filters = {}, scope = "") {
    const visible = filterAttention(data.attention.items, filters);
    return JSON.stringify({ available: data.available, demo: data.demo, total: data.attention.total, shown: data.attention.shown, partial: data.attention.partial, severity: filters.severity || "", category: filters.category || "", scope, items: visible.map((entry) => [entry.id, entry.severity, entry.category, entry.title, entry.detail, entry.occurred_at, entry.target.type, entry.target.id, entry.record_label]) });
  }
  function diagnosticsRows(diagnostics) {
    if (!diagnostics) return [];
    const build = diagnostics.build || {}, runtime = diagnostics.runtime || {}, coverage = diagnostics.coverage || {}, telemetry = diagnostics.telemetry || {};
    return [
      ["Build", [text(build.version), build.commit ? `commit ${build.commit}` : "commit not recorded"].join(" · ")],
      ["Runtime", `${runtime.available ? "Available" : "Unavailable"} · schema ${text(runtime.schema_version)} / expected ${text(runtime.expected_schema_version)}`],
      ["Emergency stop", runtime.emergency_stopped ? "Recorded as active" : "Not recorded as active"],
      ["Coverage scope", coverage.goal_id ? `Goal ${coverage.goal_id}` : "Current portal scope"],
      ["Agents", `${number(coverage.loaded_agents)} loaded · ${number(coverage.measured_agents)} measured`],
      ["Gaps", `${number(coverage.unknown_usage_agents)} unknown usage · ${number(coverage.missing_model_agents)} missing model · ${number(coverage.missing_job_agents)} missing job`],
      ["Telemetry", telemetry.last_observed_at ? `Last recorded ${time(telemetry.last_observed_at)}` : "No recorded observation timestamp"],
      ["Coverage", coverage.partial ? "Partial coverage" : "Coverage not marked partial"],
    ];
  }
  function create(doc, options = {}) {
    const $ = (id) => doc.getElementById(id), make = (tag, className, value) => { const node = doc.createElement(tag); if (className) node.className = className; if (value != null) node.textContent = String(value); return node; };
    const state = { severity: "", category: "", attentionSignature: null, categorySignature: null, diagnosticSignature: null };
    let latestData = normalize(null);
    const onTarget = typeof options.onTarget === "function" ? options.onTarget : () => {};
    function syncCategories(items) {
      const select = $("attention-category-filter"); if (!select || doc.activeElement === select) return;
      const previous = state.category, categories = [...new Set(items.map((entry) => entry.category))].sort((a, b) => a.localeCompare(b)), signature = JSON.stringify(categories);
      if (signature === state.categorySignature) return;
      state.categorySignature = signature;
      select.replaceChildren(make("option", "", "All categories")); select.firstChild.value = "";
      categories.forEach((category) => { const option = make("option", "", title(category)); option.value = category; select.append(option); });
      if (!categories.includes(previous)) state.category = ""; select.value = state.category;
    }
    function renderAttention(data, scope) {
      const visible = filterAttention(data.attention.items, state), displayScope = data.demo ? "Representative browser-only sample" : scope || "Recorded project scope", signature = attentionSignature(data, state, displayScope);
      if (signature === state.attentionSignature) return;
      state.attentionSignature = signature;
      $("attention-total").textContent = number(data.attention.total);
      $("attention-scope").textContent = displayScope;
      const list = $("attention-list"), priorScroll = list.scrollTop;
      const focusedAction = list.contains(doc.activeElement) ? doc.activeElement.closest("[data-attention-item]") : null;
      const focusedId = focusedAction?.dataset.attentionItem;
      list.replaceChildren();
      visible.forEach((entry) => {
        const row = make("li", `attention-item attention-${entry.severity}`), copy = make("div", "attention-copy"), action = make("button", "text-button attention-action", entry.target.type === "diagnostics" ? "View diagnostics" : `View ${entry.target.type}`);
        action.type = "button"; action.dataset.attentionTarget = entry.target.type; action.dataset.attentionId = entry.target.id || ""; action.dataset.attentionItem = entry.id;
        copy.append(make("div", "attention-title", entry.title), make("p", "attention-detail", entry.detail), make("div", "attention-meta", `${entry.record_label || ""} · ${title(entry.category)} · ${time(entry.occurred_at)}`));
        row.append(make("span", `status status-${entry.severity}`, title(entry.severity)), copy, action); list.append(row);
      });
      list.scrollTop = priorScroll;
      if (focusedId) {
        const restored = [...list.querySelectorAll("[data-attention-item]")].find((button) => button.dataset.attentionItem === focusedId);
        (restored || list).focus({ preventScroll: true });
      }
      $("attention-empty").hidden = visible.length > 0;
      $("attention-summary").textContent = !data.available ? "Attention data is unavailable for this snapshot." : `${number(visible.length)} of ${number(data.attention.shown)} loaded item(s) shown${data.attention.partial ? "; coverage is partial" : ""}.`;
    }
    function renderDiagnostics(data) {
      const diagnostic = data.diagnostics, signature = JSON.stringify(diagnostic || null); if (signature === state.diagnosticSignature) return; state.diagnosticSignature = signature;
      const content = $("diagnostics-content"), validation = $("diagnostics-validation"); content.replaceChildren(); validation.replaceChildren();
      if (!diagnostic) { content.append(make("p", "efficiency-note", data.demo ? "Representative demo diagnostics are unavailable." : "Diagnostics are unavailable for this snapshot.")); return; }
      const grid = make("div", "diagnostics-grid"); diagnosticsRows(diagnostic).forEach(([label, value]) => { const cell = make("div", "detail-item"); cell.append(make("b", "", label), make("span", "", value)); grid.append(cell); }); content.append(grid);
      const report = diagnostic.validation || {}, heading = make("h3", "", "Latest validation (project-wide)"); validation.append(heading);
      validation.append(make("p", "efficiency-note", report.available ? `${title(report.status)} · started ${time(report.started_at)} · finished ${time(report.finished_at)}${report.partial ? " · partial" : ""}` : "No retained validation report is available."));
      if (Array.isArray(report.checks) && report.checks.length) { const checks = make("ol", "diagnostics-checks"); report.checks.forEach((check) => checks.append(make("li", "", `${text(check.index, "check")} · ${title(check.status)} · exit ${text(check.exit_code)} · ${number(check.elapsed_ms)} ms`))); validation.append(checks); }
    }
    function update(snapshot, updateOptions = {}) { const data = normalize(snapshot?.insights, { demo: Boolean(updateOptions.demo) }); data.attention.items.forEach((entry) => { entry.record_label = attentionContext(entry, updateOptions.demo ? null : snapshot); }); latestData = data; syncCategories(data.attention.items); renderAttention(data, updateOptions.scope); renderDiagnostics(data); }
    function bind() {
      $("attention-severity-filter").addEventListener("change", (event) => { state.severity = event.target.value; renderAttention(latestData, options.scope?.()); });
      $("attention-category-filter").addEventListener("change", (event) => { state.category = event.target.value; renderAttention(latestData, options.scope?.()); });
      $("attention-clear-filters").addEventListener("click", () => { state.severity = ""; state.category = ""; $("attention-severity-filter").value = ""; $("attention-category-filter").value = ""; renderAttention(latestData, options.scope?.()); });
      $("attention-list").addEventListener("click", (event) => { const button = event.target.closest("[data-attention-target]"); if (button) onTarget({ type: button.dataset.attentionTarget, id: button.dataset.attentionId || null, itemId: button.dataset.attentionItem || null }); });
    }
    const getFilters = () => ({ severity: state.severity, category: state.category });
    const setFilters = (next = {}) => {
      state.severity = SEVERITIES.has(next.severity) ? next.severity : "";
      state.category = typeof next.category === "string" ? next.category : "";
      $("attention-severity-filter").value = state.severity;
      $("attention-category-filter").value = state.category;
      state.attentionSignature = null;
      renderAttention(latestData, options.scope?.());
    };
    return { update, bind, state, getFilters, setFilters };
  }
  return { normalize, attentionContext, filterAttention, attentionSignature, diagnosticsRows, create };
});
