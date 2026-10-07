"use strict";
const assert = require("node:assert/strict");
const { normalize, attentionContext, filterAttention, attentionSignature, diagnosticsRows } = require("../src/tasktra/portal_static/portal-insights.js");
let checks = 0;
function check(name, fn) { fn(); checks += 1; console.log("PASS " + name); }
const insights = { version: 1,
  attention: { total: 4, shown: 3, partial: true, items: [
    { id: "blocked", severity: "error", category: "blocked_job", title: "Blocked", detail: "Needs a decision", goal_id: "g", job_id: "j", target: { type: "job", id: "j" } },
    { id: "usage", severity: "info", category: "usage", title: "Usage unknown", detail: "No receipt", work_id: "w", target: { type: "agent", id: "w" } },
    { id: "coverage", severity: "warning", category: "coverage", title: "Partial", detail: "Bounded read", target: { type: "diagnostics", id: null } },
  ] },
  diagnostics: { build: { version: "1.0", commit: null }, runtime: { available: true, schema_version: 2, expected_schema_version: 2, emergency_stopped: false }, coverage: { goal_id: "g", loaded_agents: 3, measured_agents: 2, unknown_usage_agents: 1, missing_model_agents: 1, missing_job_agents: 0, partial: true }, telemetry: { last_observed_at: "2026-01-01T00:00:00Z" }, validation: { available: true, report_id: "report", status: "failed", started_at: "2026-01-01T00:00:00Z", finished_at: null, checks: [{ index: "portal", status: "failed", exit_code: 1, elapsed_ms: 12 }], partial: true } },
};
check("normalizes bounded safe attention records without inventing targets", () => {
  const data = normalize(insights);
  assert.equal(data.attention.items.length, 3); assert.equal(data.attention.total, 4); assert.equal(data.attention.partial, true);
  assert.deepEqual(data.attention.items[0].target, { type: "job", id: "j" });
  assert.equal(data.attention.items[1].target.type, "agent");
});
check("filters severity and category independently", () => {
  const items = normalize(insights).attention.items;
  assert.deepEqual(filterAttention(items, { severity: "warning" }).map((item) => item.id), ["coverage"]);
  assert.deepEqual(filterAttention(items, { category: "usage" }).map((item) => item.id), ["usage"]);
  assert.equal(filterAttention(items, { severity: "error", category: "usage" }).length, 0);
});
check("malformed or unsupported insight envelopes remain unavailable", () => {
  for (const raw of [{}, { version: 2, attention: {}, diagnostics: {} }, { version: 1, attention: { items: [] } }, { version: 1, attention: { items: "bad" }, diagnostics: [] }, { ...insights, attention: [] }, { ...insights, diagnostics: { ...insights.diagnostics, validation: [] } }]) {
    const data = normalize(raw); assert.equal(data.available, false); assert.equal(data.attention.items.length, 0); assert.equal(data.diagnostics, null);
  }
});
check("valid item coercion never invents an unsupported target", () => {
  const data = normalize({ version: 1, attention: { items: [{ severity: "secret", target: { type: "file", id: "/private" } }] }, diagnostics: insights.diagnostics });
  assert.equal(data.attention.items[0].severity, "info"); assert.deepEqual(data.attention.items[0].target, { type: "diagnostics", id: "/private" });
});
check("equivalent polling snapshots produce a stable attention render signature", () => {
  const data = normalize(insights), filters = { severity: "", category: "" };
  const first = attentionSignature(data, filters, "Recorded project scope");
  const second = attentionSignature(data, filters, "Recorded project scope");
  assert.equal(first, second); assert.equal(first.includes("attentionSignature"), false);
});
check("diagnostics retain project-wide validation failure and partial coverage", () => {
  const rows = diagnosticsRows(normalize(insights).diagnostics);
  assert.ok(rows.some(([label, value]) => label === "Coverage" && value === "Partial coverage"));
  assert.ok(rows.some(([label, value]) => label === "Runtime" && /Available/.test(value)));
});
check("demo is synthetic and never reuses live diagnostic values", () => {
  const data = normalize(insights, { demo: true });
  assert.equal(data.demo, true); assert.equal(data.diagnostics.build.version, "demo"); assert.notEqual(data.attention.items[0].title, "Blocked");
});
check("attention context resolves canonical runs despite reused reported agent IDs", () => {
  const entry = { target: { type: "agent", id: "second" } };
  const snapshot = { agents: [{ id: "same", work_id: "first", role: "Wrong" }, { id: "same", work_id: "second", role: "Reviewer" }] };
  assert.equal(attentionContext(entry, snapshot), "Reviewer · agent second");
  assert.equal(attentionContext(entry, { agents: [] }), "agent second");
});
check("renamed linked records invalidate attention context without changing the alert", () => {
  const data = normalize(insights), entry = data.attention.items[0];
  entry.record_label = attentionContext(entry, { jobs: [{ id: "j", title: "Before" }] });
  const before = attentionSignature(data);
  entry.record_label = attentionContext(entry, { jobs: [{ id: "j", title: "After" }] });
  assert.notEqual(attentionSignature(data), before);
  assert.match(entry.record_label, /After/);
});
console.log(`portal insights checks passed (${checks})`);
