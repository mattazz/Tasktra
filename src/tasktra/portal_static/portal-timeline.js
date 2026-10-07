(function(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.TasktraPortalTimeline = api;
})(typeof window !== "undefined" ? window : globalThis, function() {
  "use strict";
  const MAX_ROWS = 500;
  const KINDS = new Set(["job_attempt", "agent"]);
  const TIMINGS = new Set(["recorded_duration", "open_attempt", "observation_window", "unknown"]);
  const TARGETS = new Set(["job", "agent"]);
  const safeText = (value) => String(value == null ? "" : value);
  const time = (value) => {
    if (!value || typeof value !== "string") return null;
    const ms = Date.parse(value);
    return Number.isFinite(ms) ? ms : null;
  };
  const number = (value) => Number.isFinite(value) ? new Intl.NumberFormat().format(value) : "0";
  const localTime = (value) => {
    const ms = time(value);
    return ms == null ? "Not recorded" : new Date(ms).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  };
  const title = (value) => safeText(value || "unknown").replace(/[-_]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
  const stateKey = (value) => safeText(value || "unknown").toLowerCase().replace(/[^a-z0-9-]+/g, "-");

  const isPlainObject = (value) => Boolean(value) && typeof value === "object" && !Array.isArray(value) && (Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null);
  const nonnegativeInteger = (value) => Number.isSafeInteger(value) && value >= 0;
  const identifier = (value) => typeof value === "string" && value.trim() ? value : null;
  const timestamp = (value) => {
    if (value == null) return { valid: true, value: null, ms: null };
    if (typeof value !== "string") return { valid: false, value: null, ms: null };
    const ms = Date.parse(value);
    return Number.isFinite(ms) ? { valid: true, value, ms } : { valid: false, value: null, ms: null };
  };
  const optionalText = (value) => value == null ? null : typeof value === "string" ? value : null;

  function normalizeRow(row) {
    if (!isPlainObject(row) || !KINDS.has(row.kind) || !TIMINGS.has(row.timing) || !isPlainObject(row.target)) return null;
    const rowId = identifier(row.id), targetId = identifier(row.target.id), targetType = row.target.type;
    if (!rowId || !targetId || !TARGETS.has(targetType) || (row.kind === "job_attempt" ? targetType !== "job" : targetType !== "agent")) return null;
    const started = timestamp(row.started_at), ended = timestamp(row.ended_at), observed = timestamp(row.last_observed_at);
    if (!started.valid || !ended.valid || !observed.valid) return null;
    const duration = row.duration_ms == null ? null : Number.isFinite(row.duration_ms) && row.duration_ms >= 0 ? row.duration_ms : null;
    if (row.duration_ms != null && duration == null) return null;
    const hasExecutionDuration = duration != null;
    if (row.timing === "recorded_duration" && (started.ms == null || ended.ms == null || started.ms > ended.ms || !hasExecutionDuration)) return null;
    if (row.timing === "open_attempt" && (started.ms == null || ended.ms != null || hasExecutionDuration)) return null;
    if (row.timing === "observation_window" && (ended.ms != null || hasExecutionDuration || (started.ms == null && observed.ms == null) || (started.ms != null && observed.ms != null && started.ms > observed.ms))) return null;
    if (row.timing === "unknown" && (started.ms != null || ended.ms != null || observed.ms != null || hasExecutionDuration)) return null;
    const label = optionalText(row.label), state = optionalText(row.state), model = optionalText(row.model), role = optionalText(row.role);
    if ((row.label != null && label == null) || (row.state != null && state == null) || (row.model != null && model == null) || (row.role != null && role == null)) return null;
    return {
      id: rowId, kind: row.kind, label: label || rowId, goal_id: optionalText(row.goal_id), job_id: optionalText(row.job_id), work_id: optionalText(row.work_id), state, model, role,
      started_at: started.value, ended_at: ended.value, last_observed_at: observed.value, duration_ms: duration, timing: row.timing, target: { type: targetType, id: targetId },
    };
  }
  function unavailableTimeline() { return { available: false, rows: [], total: 0, shown: 0, partial: false, start_at: null, end_at: null, invalid_rows: 0 }; }
  function normalizeTimeline(raw) {
    if (!isPlainObject(raw) || raw.version !== 1 || !Array.isArray(raw.rows) || !nonnegativeInteger(raw.total) || !nonnegativeInteger(raw.shown) || typeof raw.partial !== "boolean" || raw.shown !== raw.rows.length || raw.total < raw.shown || (raw.total > raw.shown && !raw.partial)) return unavailableTimeline();
    const start = timestamp(raw.start_at), end = timestamp(raw.end_at);
    if (!start.valid || !end.valid || (start.ms != null && end.ms != null && start.ms > end.ms)) return unavailableTimeline();
    const seen = new Set(), rows = []; let invalidRows = 0;
    raw.rows.slice(0, MAX_ROWS).forEach((source) => { const row = normalizeRow(source); if (!row || seen.has(row?.id)) { invalidRows++; return; } seen.add(row.id); rows.push(row); });
    invalidRows += Math.max(0, raw.rows.length - MAX_ROWS);
    return { available: true, rows, total: raw.total, shown: raw.shown, partial: raw.partial || invalidRows > 0, start_at: start.value, end_at: end.value, invalid_rows: invalidRows };
  }
  function rowStart(row) { return time(row.started_at) ?? time(row.last_observed_at); }
  function rowEnd(row) { return time(row.ended_at) ?? time(row.last_observed_at) ?? rowStart(row); }
  function extent(rows) {
    const marks = rows.flatMap((row) => [rowStart(row), rowEnd(row)]).filter(Number.isFinite);
    return marks.length ? { start: Math.min(...marks), end: Math.max(...marks) } : null;
  }
  function filterRows(rows, filters = {}) {
    return (rows || []).filter((row) => (!filters.kind || row.kind === filters.kind) && (!filters.state || safeText(row.state) === filters.state) && (!filters.model || safeText(row.model) === filters.model));
  }
  function timingLabel(row) {
    if (row.timing === "recorded_duration") return `Recorded duration: ${Math.round(row.duration_ms / 1000)} seconds`;
    if (row.timing === "open_attempt") return "Open attempt; no finish timestamp recorded";
    if (row.timing === "observation_window") return row.started_at && row.last_observed_at ? "Observation window; this is not a runtime duration" : "Observed time; this is not a runtime duration";
    return "Timestamps not recorded";
  }
  function create(doc, options = {}) {
    const byId = (id) => doc.getElementById(id), make = (tag, className, text) => { const node = doc.createElement(tag); if (className) node.className = className; if (text != null) node.textContent = text; return node; };
    const controls = { kind: byId("timeline-kind-filter"), state: byId("timeline-state-filter"), model: byId("timeline-model-filter") };
    const rowsNode = byId("timeline-rows"), unknownNode = byId("timeline-unknown-list"), empty = byId("timeline-empty"), axis = byId("timeline-axis"), coverage = byId("timeline-coverage");
    let filters = { kind: "", state: "", model: "" }, signature = "", optionSignatures = { state: "", model: "" };
    const setOptions = (select, values, label) => {
      const current = select.value, fragment = doc.createDocumentFragment(), blank = make("option", "", label); blank.value = ""; fragment.append(blank);
      values.forEach((value) => { const option = make("option", "", value); option.value = value; fragment.append(option); });
      select.replaceChildren(fragment); select.value = values.includes(current) ? current : "";
    };
    const syncOptions = (key, values, label) => {
      const select = controls[key], next = JSON.stringify(values);
      if (next === optionSignatures[key]) return;
      if (doc.activeElement === select) return;
      setOptions(select, values, label); optionSignatures[key] = next;
      if (filters[key] !== select.value) { filters[key] = select.value; signature = ""; }
    };
    const renderRow = (row, timelineExtent) => {
      const item = make("li", `timeline-row timeline-${row.timing}`), button = make("button", "timeline-row-button");
      button.type = "button"; button.dataset.timelineId = row.id; button.dataset.timelineTarget = row.target.type; button.dataset.timelineTargetId = row.target.id;
      button.setAttribute("aria-label", `Open ${row.kind === "agent" ? "agent" : "job"}: ${row.label}`);
      const header = make("span", "timeline-row-header"); header.append(make("strong", "", row.label), make("span", "timeline-row-meta", `${title(row.kind)} \u00b7 ${title(row.state)}${row.model ? ` \u00b7 ${row.model}` : ""}`));
      const track = make("span", "timeline-track"), mark = make("span", `timeline-mark timeline-mark-${row.timing}`);
      const start = rowStart(row), end = rowEnd(row);
      if (timelineExtent && start != null) {
        const width = Math.max(1, timelineExtent.end - timelineExtent.start), left = Math.max(0, Math.min(100, (start - timelineExtent.start) / width * 100)), finish = end == null ? start : end;
        const right = Math.max(left, Math.min(100, (finish - timelineExtent.start) / width * 100));
        mark.style.left = `${left}%`; mark.style.width = `${Math.max(1.5, right - left)}%`;
      }
      track.append(mark); button.append(header, track, make("span", "timeline-row-timing", `${timingLabel(row)} \u00b7 ${row.started_at ? `Start ${localTime(row.started_at)}` : row.last_observed_at ? `Observed ${localTime(row.last_observed_at)}` : "No timestamp"}`));
      button.addEventListener("click", () => options.onTarget?.(row.target)); item.append(button); return item;
    };
    const update = (snapshot, context = {}) => {
      const report = normalizeTimeline(snapshot?.insights?.timeline);
      const states = [...new Set(report.rows.map((row) => row.state).filter(Boolean))].sort(), models = [...new Set(report.rows.map((row) => row.model).filter(Boolean))].sort();
      syncOptions("state", states, "All states"); syncOptions("model", models, "All models");
      const rows = filterRows(report.rows, filters), known = rows.filter((row) => rowStart(row) != null), unknown = rows.filter((row) => rowStart(row) == null), bounds = extent(known);
      const next = JSON.stringify([report, filters, context.scope || "", context.demo ? "demo" : "live"]);
      if (next === signature) return;
      const focusedId = doc.activeElement?.dataset?.timelineId || "";
      signature = next;
      const scope = context.scope || "Recorded project scope";
      coverage.textContent = !report.available ? "Timeline data is unavailable for this snapshot." : `${number(rows.length)} valid record${rows.length === 1 ? "" : "s"} shown from ${number(report.shown)} loaded of ${number(report.total)} recorded in ${scope}.${report.partial ? ` Coverage is partial${report.invalid_rows ? `; ${number(report.invalid_rows)} malformed or duplicate row${report.invalid_rows === 1 ? " was" : "s were"} omitted` : ""}.` : ""}`;
      axis.replaceChildren();
      if (bounds) { axis.append(make("span", "", localTime(new Date(bounds.start).toISOString())), make("span", "", localTime(new Date(bounds.end).toISOString()))); axis.hidden = false; } else axis.hidden = true;
      const knownScrollTop = rowsNode.scrollTop, unknownScrollTop = unknownNode.scrollTop;
      rowsNode.replaceChildren(...known.map((row) => renderRow(row, bounds)));
      unknownNode.replaceChildren(...unknown.map((row) => renderRow(row, null)));
      rowsNode.scrollTop = knownScrollTop; unknownNode.scrollTop = unknownScrollTop;
      empty.hidden = known.length > 0 || unknown.length > 0;
      byId("timeline-unknown-heading").parentElement.hidden = !unknown.length;
      if (focusedId) { const replacement = [...rowsNode.querySelectorAll("[data-timeline-id]"), ...unknownNode.querySelectorAll("[data-timeline-id]")].find((node) => node.dataset.timelineId === focusedId); replacement?.focus({ preventScroll: true }); }
    };
    const bind = () => {
      Object.entries(controls).forEach(([key, select]) => { select.addEventListener("change", () => { filters[key] = select.value; signature = ""; update(options.getSnapshot?.() || {}, options.getContext?.() || {}); }); if (key !== "kind") select.addEventListener("blur", () => { optionSignatures[key] = ""; update(options.getSnapshot?.() || {}, options.getContext?.() || {}); }); });
      byId("timeline-clear-filters").addEventListener("click", () => { filters = { kind: "", state: "", model: "" }; Object.values(controls).forEach((select) => { select.value = ""; }); signature = ""; update(options.getSnapshot?.() || {}, options.getContext?.() || {}); });
    };
    const setFilters = (next = {}) => { filters = { kind: next.kind || "", state: next.state || "", model: next.model || "" }; Object.entries(controls).forEach(([key, select]) => { select.value = filters[key]; }); signature = ""; update(options.getSnapshot?.() || {}, options.getContext?.() || {}); };
    return { bind, update, getFilters: () => ({ ...filters }), setFilters };
  }
  return { normalizeTimeline, filterRows, extent, timingLabel, create };
});
