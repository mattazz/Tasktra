(function(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.TasktraPortalOutcomes = api;
})(typeof window !== "undefined" ? window : globalThis, function() {
  "use strict";
  const MAX_JOBS = 500, MAX_CHECKS = 100, MAX_DELIVERABLES = 100;
  const text = (value) => String(value == null ? "" : value);
  const isObject = (value) => Boolean(value) && typeof value === "object" && !Array.isArray(value) && (Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null);
  const string = (value, max = 400) => typeof value === "string" && value.length <= max ? value : null;
  const identifier = (value) => { const result = string(value, 256); return result && result.trim() ? result : null; };
  const integer = (value) => Number.isSafeInteger(value) && value >= 0;
  const amount = (value) => value == null ? null : Number.isFinite(value) && value >= 0 ? value : null;
  const timestamp = (value) => { if (value == null) return null; return typeof value === "string" && Number.isFinite(Date.parse(value)) ? value : undefined; };
  const number = (value) => Number.isFinite(value) ? new Intl.NumberFormat().format(value) : "Unavailable";
  const percent = (value) => Number.isFinite(value) ? `${Math.round(value * 100)}%` : "Unavailable";
  const title = (value) => text(value || "unknown").replace(/[-_]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
  const validUrl = (value) => { if (typeof value !== "string" || /[\u0000-\u001f\u007f]/.test(value)) return null; try { const url = new URL(value); if (url.protocol !== "https:" || url.port || url.username || url.password || url.search || url.hash || url.hostname !== "github.com") return null; const owner = "[A-Za-z0-9_-][A-Za-z0-9_.-]*", commit = new RegExp(`^\/${owner}\/${owner}\/commit\/[a-f0-9]{40}$`, "i").test(url.pathname), pull = new RegExp(`^\/${owner}\/${owner}\/pull\/[1-9][0-9]*$`).test(url.pathname), action = new RegExp(`^\/${owner}\/${owner}\/actions\/runs\/[1-9][0-9]*$`).test(url.pathname); return commit || pull || action ? url.href : null; } catch (_) { return null; } };
  function normalizeEvidence(source) {
    if (!isObject(source)) return null;
    const hash = source.workflow_sha256 == null ? null : typeof source.workflow_sha256 === "string" && /^[a-f0-9]{64}$/i.test(source.workflow_sha256) ? source.workflow_sha256 : null;
    if (!Array.isArray(source.checks) || !Array.isArray(source.deliverables) || typeof source.partial !== "boolean") return null;
    let partial = source.partial, invalid = 0;
    const checks = source.checks.slice(0, MAX_CHECKS).flatMap((check) => { if (!isObject(check) || !string(check.name, 240) || !string(check.status, 80) || (check.exit_code != null && !Number.isSafeInteger(check.exit_code)) || (check.elapsed_ms != null && !integer(check.elapsed_ms))) { invalid++; return []; } return [{ name: check.name, status: check.status, exit_code: check.exit_code ?? null, elapsed_ms: check.elapsed_ms ?? null }]; });
    invalid += Math.max(0, source.checks.length - MAX_CHECKS);
    const deliverables = source.deliverables.slice(0, MAX_DELIVERABLES).flatMap((entry) => { if (!isObject(entry) || !["commit", "path", "link"].includes(entry.kind) || !string(entry.value, 500)) { invalid++; return []; } if (entry.kind === "commit" && !/^[a-f0-9]{40}$/i.test(entry.value)) { invalid++; return []; } if (entry.kind === "path" && (!/^[A-Za-z0-9][A-Za-z0-9._/ -]*$/.test(entry.value) || entry.value.includes(".."))) { invalid++; return []; } if (entry.kind === "link" && !validUrl(entry.value)) { invalid++; return []; } return [{ kind: entry.kind, value: entry.kind === "link" ? validUrl(entry.value) : entry.value }]; });
    invalid += Math.max(0, source.deliverables.length - MAX_DELIVERABLES);
    return { workflow_sha256: hash, checks, deliverables, partial: partial || invalid > 0, invalid };
  }
  function normalizeJob(source) {
    if (!isObject(source) || !identifier(source.job_id) || !identifier(source.goal_id) || !string(source.title, 400) || !string(source.state, 100) || !isObject(source.verification) || !isObject(source.usage)) return null;
    const verification = source.verification, recordedAt = timestamp(verification.recorded_at), policy = verification.policy == null ? null : string(verification.policy, 160), reason = verification.reason == null ? null : string(verification.reason, 400);
    if (typeof verification.verified !== "boolean" || recordedAt === undefined || (verification.policy != null && !policy) || (verification.reason != null && !reason)) return null;
    const evidence = normalizeEvidence(source.evidence); if (!evidence) return null;
    const state = source.state.toLowerCase(), complete = ["complete", "completed", "succeeded", "success"].includes(state);
    if (verification.verified && (!complete || !policy || !evidence.workflow_sha256)) return null;
    const numeric = ["attempt_count", "retry_count", "timed_attempts", "attempts_shown"].every((key) => integer(source[key]));
    const duration = amount(source.attempt_duration_ms); if (!numeric || (source.attempt_duration_ms != null && duration == null) || source.retry_count > Math.max(0, source.attempt_count - 1) || source.timed_attempts > source.attempts_shown || source.attempts_shown > source.attempt_count || typeof source.attempts_partial !== "boolean") return null;
    const usage = source.usage, usageKeys = ["total_tokens", "input_tokens", "cached_input_tokens", "output_tokens"], countKeys = ["measured_runs", "unknown_runs", "attributed_runs"];
    if (!usageKeys.every((key) => amount(usage[key]) !== null || usage[key] === null) || !countKeys.every((key) => integer(usage[key])) || typeof usage.partial !== "boolean" || usage.attributed_runs !== usage.measured_runs + usage.unknown_runs) return null;
    if (usage.measured_runs === 0 && usageKeys.some((key) => usage[key] != null)) return null;
    if (usage.measured_runs > 0 && usageKeys.some((key) => usage[key] == null)) return null;
    if (usage.total_tokens != null && usage.input_tokens + usage.output_tokens !== usage.total_tokens) return null;
    if (usage.cached_input_tokens != null && usage.input_tokens != null && usage.cached_input_tokens > usage.input_tokens) return null;
    return { job_id: source.job_id, goal_id: source.goal_id, title: source.title, state: source.state, verification: { verified: verification.verified, policy, recorded_at: recordedAt, reason }, attempt_count: source.attempt_count, retry_count: source.retry_count, attempt_duration_ms: duration, timed_attempts: source.timed_attempts, attempts_shown: source.attempts_shown, attempts_partial: source.attempts_partial, usage: { total_tokens: usage.total_tokens, input_tokens: usage.input_tokens, cached_input_tokens: usage.cached_input_tokens, output_tokens: usage.output_tokens, measured_runs: usage.measured_runs, unknown_runs: usage.unknown_runs, attributed_runs: usage.attributed_runs, partial: usage.partial }, evidence };
  }
  function normalizeSummary(source) {
    if (!isObject(source)) return null; const counts = ["completed_jobs", "verified_jobs", "measured_verified_jobs", "attributable_runs", "unattributed_runs", "unknown_usage_runs"];
    if (!counts.every((key) => integer(source[key])) || typeof source.partial !== "boolean" || source.verified_jobs > source.completed_jobs || source.measured_verified_jobs > source.verified_jobs || (source.measured_tokens_for_verified_jobs != null && amount(source.measured_tokens_for_verified_jobs) == null) || (source.tokens_per_measured_verified_job != null && amount(source.tokens_per_measured_verified_job) == null)) return null;
    if (source.unknown_usage_runs > source.attributable_runs || (source.measured_verified_jobs === 0 && (source.measured_tokens_for_verified_jobs != null || source.tokens_per_measured_verified_job != null)) || (source.measured_verified_jobs > 0 && (source.measured_tokens_for_verified_jobs == null || source.tokens_per_measured_verified_job == null))) return null;
    if (source.measured_verified_jobs > 0 && source.tokens_per_measured_verified_job !== source.measured_tokens_for_verified_jobs / source.measured_verified_jobs) return null;
    return { ...source };
  }
  function normalizeModel(source) {
    if (!isObject(source) || !string(source.model, 200) || !integer(source.measured_runs) || !integer(source.verified_jobs) || amount(source.total_tokens) == null || amount(source.failed_tokens) == null || (source.tokens_per_measured_run != null && amount(source.tokens_per_measured_run) == null) || (source.cache_fraction != null && (!Number.isFinite(source.cache_fraction) || source.cache_fraction < 0 || source.cache_fraction > 1))) return null;
    if (!source.measured_runs || source.verified_jobs > source.measured_runs || source.failed_tokens > source.total_tokens || source.tokens_per_measured_run !== source.total_tokens / source.measured_runs) return null;
    return { ...source };
  }
  const unavailable = () => ({ available: false, jobs: [], total: 0, shown: 0, partial: false, summary: null, by_model: [], invalid_jobs: 0 });
  function normalizeOutcomes(raw) {
    if (!isObject(raw) || raw.version !== 1 || !Array.isArray(raw.jobs) || !integer(raw.total) || !integer(raw.shown) || typeof raw.partial !== "boolean" || raw.shown !== raw.jobs.length || raw.total < raw.shown || (raw.total > raw.shown && !raw.partial)) return unavailable();
    const summary = normalizeSummary(raw.summary); if (!summary || !Array.isArray(raw.by_model)) return unavailable();
    const ids = new Set(), jobs = []; let invalid = 0;
    raw.jobs.slice(0, MAX_JOBS).forEach((source) => { const job = normalizeJob(source); if (!job || ids.has(job?.job_id)) { invalid++; return; } ids.add(job.job_id); jobs.push(job); }); invalid += Math.max(0, raw.jobs.length - MAX_JOBS);
    const modelIds = new Set(); let duplicateModel = false;
    const by_model = raw.by_model.slice(0, MAX_JOBS).flatMap((source) => { const model = normalizeModel(source); if (!model) return []; if (modelIds.has(model.model)) { duplicateModel = true; return []; } modelIds.add(model.model); return [model]; });
    if (duplicateModel) return unavailable();
    const partial = raw.partial || summary.partial || invalid > 0 || by_model.length !== raw.by_model.length;
    const completed = jobs.filter((job) => ["complete", "completed", "succeeded", "success"].includes(job.state.toLowerCase())), verified = jobs.filter((job) => job.verification.verified), measured = verified.filter((job) => job.usage.measured_runs > 0), attributed = verified.reduce((total, job) => total + job.usage.attributed_runs, 0), unknown = verified.reduce((total, job) => total + job.usage.unknown_runs, 0), tokens = measured.length ? measured.reduce((total, job) => total + job.usage.total_tokens, 0) : null, measuredRuns = verified.reduce((total, job) => total + job.usage.measured_runs, 0);
    if (!partial && (summary.completed_jobs !== completed.length || summary.verified_jobs !== verified.length || summary.measured_verified_jobs !== measured.length || summary.attributable_runs !== attributed || summary.unknown_usage_runs !== unknown || summary.measured_tokens_for_verified_jobs !== tokens || summary.tokens_per_measured_verified_job !== (measured.length ? tokens / measured.length : null))) return unavailable();
    if (by_model.some((model) => model.measured_runs > measuredRuns || model.verified_jobs > verified.length) || by_model.reduce((total, model) => total + model.measured_runs, 0) > measuredRuns) return unavailable();
    const modelRuns = by_model.reduce((total, model) => total + model.measured_runs, 0), modelTokens = by_model.reduce((total, model) => total + model.total_tokens, 0);
    if (modelTokens > (tokens ?? 0) || (!partial && (modelRuns !== measuredRuns || modelTokens !== (tokens ?? 0)))) return unavailable();
    return { available: true, jobs, total: raw.total, shown: raw.shown, partial, summary, by_model, invalid_jobs: invalid };
  }
  function jobEvidence(doc, job) {
    const make = (tag, className, value) => { const node = doc.createElement(tag); if (className) node.className = className; if (value != null) node.textContent = value; return node; }, item = (label, value) => { const node = make("div", "detail-item"); node.append(make("b", "", label), make("span", "", value)); return node; };
    const panel = make("section", "panel job-evidence-panel"); panel.id = "job-evidence-panel";
    if (!job) { panel.append(make("h3", "", "Evidence and verification"), make("p", "efficiency-note", "No recorded outcome evidence is available for this job in the current goal scope.")); return panel; }
    const verified = job.verification.verified ? `Verified under recorded policy: ${job.verification.policy || "not recorded"}` : "Unverified";
    const grid = make("div", "detail-grid"); grid.append(item("Verification", verified), item("Recorded reason", job.verification.reason || "No verification reason recorded"), item("Attempts / retries", `${number(job.attempt_count)} / ${number(job.retry_count)}`), item("Recorded attempt time", job.attempt_duration_ms == null ? "No completed duration recorded" : `${Math.round(job.attempt_duration_ms / 1000)} seconds`), item("Timed / loaded attempts", `${number(job.timed_attempts)} / ${number(job.attempts_shown)}${job.attempts_partial ? " (partial)" : ""}`), item("Linked run usage", `${number(job.usage.measured_runs)} measured, ${number(job.usage.unknown_runs)} unknown of ${number(job.usage.attributed_runs)} attributed`), item("Attributed tokens", number(job.usage.total_tokens)), item("Workflow hash", job.evidence.workflow_sha256 || "No valid stored workflow hash"));
    panel.append(make("h3", "", "Evidence and verification"), grid);
    const checks = make("div", "outcome-evidence-list"), checksHeading = make("h4", "", "Recorded checks"); checks.append(checksHeading); if (job.evidence.checks.length) { const list = make("ul", ""); list.dataset.evidenceScroll = "checks"; job.evidence.checks.forEach((check) => list.append(make("li", "", `${check.name} \u00b7 ${title(check.status)} \u00b7 ${check.exit_code == null ? "exit not recorded" : `exit ${check.exit_code}`} \u00b7 ${check.elapsed_ms == null ? "duration not recorded" : `${check.elapsed_ms} ms`}`))); checks.append(list); } else checks.append(make("p", "efficiency-note", "No allowlisted recorded checks."));
    const deliverables = make("div", "outcome-evidence-list"), deliverableHeading = make("h4", "", "Safe deliverables"); deliverables.append(deliverableHeading); if (job.evidence.deliverables.length) { const list = make("ul", ""); list.dataset.evidenceScroll = "deliverables"; job.evidence.deliverables.forEach((entry) => { const line = make("li", ""); if (entry.kind === "link") { const link = make("a", "", entry.value); link.href = entry.value; link.target = "_blank"; link.rel = "noreferrer"; line.append(link); } else line.textContent = `${title(entry.kind)}: ${entry.value}`; list.append(line); }); deliverables.append(list); } else deliverables.append(make("p", "efficiency-note", "No safe recorded deliverables."));
    panel.append(checks, deliverables); if (job.evidence.partial || job.usage.partial) panel.append(make("p", "efficiency-note", "Evidence or usage coverage is partial; omitted records are not inferred.")); return panel;
  }
  function create(doc, options = {}) {
    const byId = (id) => doc.getElementById(id), make = (tag, className, value) => { const node = doc.createElement(tag); if (className) node.className = className; if (value != null) node.textContent = value; return node; };
    const panel = byId("outcomes-panel"), summary = byId("outcomes-summary"), metrics = byId("outcomes-metrics"), models = byId("outcomes-model-body"), jobs = byId("outcomes-jobs"); let signature = "";
    const update = (snapshot, context = {}) => { const report = normalizeOutcomes(snapshot?.insights?.outcomes), next = JSON.stringify([report, context.scope || "", context.demo ? "demo" : "live"]); if (next === signature) return; const focusedJobId = doc.activeElement?.dataset?.outcomeJobId || ""; signature = next;
      const scope = context.scope || "recorded project scope"; summary.textContent = !report.available ? `Verified outcomes are unavailable for this ${scope}.` : `${number(report.summary.verified_jobs)} verified job${report.summary.verified_jobs === 1 ? "" : "s"}; ${number(report.summary.measured_verified_jobs)} have measured usage in the same verified-job cohort. ${number(report.shown)} loaded of ${number(report.total)} recorded in ${scope}.${report.partial ? " Coverage is partial." : ""}`;
      metrics.replaceChildren(...(!report.available ? [] : [["Completed jobs", report.summary.completed_jobs], ["Verified jobs", report.summary.verified_jobs], ["Measured verified jobs", report.summary.measured_verified_jobs], ["Verified cohort tokens", report.summary.measured_tokens_for_verified_jobs], ["Tokens / measured verified job", report.summary.tokens_per_measured_verified_job], ["Attributed / unattributed runs", `${number(report.summary.attributable_runs)} / ${number(report.summary.unattributed_runs)}`], ["Unknown usage runs", report.summary.unknown_usage_runs]].map(([label, value]) => { const card = make("article", "panel efficiency-metric"); card.append(make("p", "eyebrow", label), make("strong", "efficiency-value", typeof value === "string" ? value : number(value))); return card; })));
      models.replaceChildren(...report.by_model.map((group) => { const row = make("tr"); row.append(make("td", "", group.model), make("td", "", number(group.measured_runs)), make("td", "", number(group.verified_jobs)), make("td", "", number(group.total_tokens)), make("td", "", number(group.tokens_per_measured_run)), make("td", "", number(group.failed_tokens)), make("td", "", percent(group.cache_fraction))); return row; })); if (!models.children.length) { const row = make("tr"), cell = make("td", "efficiency-note", "No attributed measured model comparison is available for this verified cohort."); cell.colSpan = 7; row.append(cell); models.append(row); }
      const jobsScrollTop = jobs.scrollTop;
      jobs.replaceChildren(...report.jobs.map((job) => { const item = make("li", "outcomes-job"), button = make("button", "text-button", `${job.title} \u00b7 ${job.verification.verified ? "verified" : "unverified"}`); button.type = "button"; button.dataset.outcomeJobId = job.job_id; button.addEventListener("click", () => options.onJob?.(job.job_id)); item.append(button); return item; }));
      jobs.scrollTop = jobsScrollTop;
      if (focusedJobId) [...jobs.querySelectorAll("[data-outcome-job-id]")].find((node) => node.dataset.outcomeJobId === focusedJobId)?.focus({ preventScroll: true });
    };
    return { update, normalize: normalizeOutcomes };
  }
  return { normalizeOutcomes, jobEvidence, create, validUrl };
});
