"use strict";
const assert = require("node:assert/strict");
const { normalizeTimeline, filterRows, extent, timingLabel } = require("../src/tasktra/portal_static/portal-timeline.js");
let checks = 0;
function check(name, fn) { fn(); checks += 1; console.log("PASS " + name); }
const raw = { version: 1, total: 4, shown: 4, partial: false, start_at: "2026-10-07T10:00:00Z", end_at: "2026-10-07T11:00:00Z", rows: [
  { id: "job:a", kind: "job_attempt", label: "Recorded attempt", state: "completed", model: "m1", started_at: "2026-10-07T10:00:00Z", ended_at: "2026-10-07T10:30:00Z", duration_ms: 1800000, timing: "recorded_duration", target: { type: "job", id: "a" } },
  { id: "agent:w", kind: "agent", label: "Observed agent", state: "working", model: "m2", started_at: "2026-10-07T10:15:00Z", ended_at: null, last_observed_at: "2026-10-07T10:50:00Z", duration_ms: null, timing: "observation_window", target: { type: "agent", id: "w" } },
  { id: "job:open", kind: "job_attempt", label: "Open attempt", state: "running", started_at: "2026-10-07T10:20:00Z", ended_at: null, timing: "open_attempt", target: { type: "job", id: "open" } },
  { id: "agent:unknown", kind: "agent", label: "Unknown time", state: "unknown", timing: "unknown", target: { type: "agent", id: "unknown" } },
] };
check("normalizes only supported timeline rows with exact targets", () => {
  const report = normalizeTimeline(raw); assert.equal(report.available, true); assert.equal(report.rows.length, 4); assert.deepEqual(report.rows[0].target, { type: "job", id: "a" });
});
check("unsupported or structurally invalid envelopes remain unavailable", () => {
  const validEmpty = { version: 1, rows: [], total: 0, shown: 0, partial: false, start_at: null, end_at: null };
  for (const envelope of [
    { ...validEmpty, version: 2 }, { ...validEmpty, total: -1 }, { ...validEmpty, shown: 1 },
    { ...validEmpty, partial: "false" }, { ...validEmpty, total: 1, partial: false },
    { ...validEmpty, start_at: "not-a-time" }, { ...validEmpty, start_at: "2026-10-07T11:00:00Z", end_at: "2026-10-07T10:00:00Z" },
  ]) assert.equal(normalizeTimeline(envelope).available, false);
});
check("malformed and duplicate rows are omitted with partial coverage instead of trusted", () => {
  const duplicate = { ...raw, rows: [raw.rows[0], { ...raw.rows[0] }], total: 2, shown: 2, partial: false };
  const report = normalizeTimeline(duplicate); assert.equal(report.available, true); assert.equal(report.rows.length, 1); assert.equal(report.partial, true); assert.equal(report.invalid_rows, 1);
  const malformed = { ...raw, rows: [{ ...raw.rows[0], target: { type: "file", id: "x" } }], total: 1, shown: 1, partial: false };
  assert.equal(normalizeTimeline(malformed).rows.length, 0); assert.equal(normalizeTimeline(malformed).partial, true);
});
check("timing variants reject contradictory timestamps and durations", () => {
  const variants = [
    { ...raw.rows[0], ended_at: "2026-10-07T09:00:00Z" },
    { ...raw.rows[0], duration_ms: null },
    { ...raw.rows[2], ended_at: "2026-10-07T10:25:00Z" },
    { ...raw.rows[2], duration_ms: 20 },
    { ...raw.rows[1], ended_at: "2026-10-07T10:50:00Z" },
    { ...raw.rows[1], duration_ms: 20 },
    { ...raw.rows[1], timing: "unknown" },
  ];
  for (const row of variants) { const report = normalizeTimeline({ ...raw, rows: [row], total: 1, shown: 1, partial: false }); assert.equal(report.rows.length, 0); assert.equal(report.partial, true); }
});
check("single observations remain explicit observations rather than invented windows", () => {
  const row = { ...raw.rows[1], id: "agent:single", started_at: null, last_observed_at: "2026-10-07T10:50:00Z" };
  const report = normalizeTimeline({ ...raw, rows: [row], total: 1, shown: 1, partial: false }); assert.equal(report.rows.length, 1); assert.match(timingLabel(report.rows[0]), /Observed time/);
});
check("type state and model filters combine without changing raw rows", () => {
  const rows = normalizeTimeline(raw).rows; assert.deepEqual(filterRows(rows, { kind: "agent", state: "working", model: "m2" }).map((row) => row.id), ["agent:w"]); assert.equal(rows.length, 4);
});
check("extent uses recorded marks only and unknown rows stay unpositioned", () => {
  const rows = normalizeTimeline(raw).rows; const result = extent(rows.filter((row) => row.id !== "agent:unknown")); assert.ok(result.end >= result.start); assert.equal(extent([rows[3]]), null);
});
check("timing copy does not imply unobserved runtime", () => {
  const rows = normalizeTimeline(raw).rows; assert.match(timingLabel(rows[1]), /not a runtime duration/); assert.match(timingLabel(rows[2]), /no finish timestamp/); assert.match(timingLabel(rows[3]), /not recorded/);
});
console.log(`portal timeline checks passed (${checks})`);
