"""Deterministic, offline HTML view for a normalized operator cockpit snapshot.

This module deliberately has no connection to Tasktra state.  The backend owns
snapshot capture and publication; this renderer only turns its supplied mapping
into a self-contained, read-only document.
"""

from __future__ import annotations

from base64 import b64encode
from hashlib import sha256
import json
from typing import Any, Mapping


CSS = r"""
:root {
  color-scheme: dark;
  font-family: Inter, ui-sans-serif, system-ui, sans-serif;
  background: #0b1020;
  color: #e7edf9;
  line-height: 1.45;
}
* {
  box-sizing: border-box;
}
body {
  margin: 0;
  min-width: 0;
  background: radial-gradient(circle at top right, #17265a 0, #0b1020 42rem);
}
button,
input,
select {
  font: inherit;
}
button {
  cursor: pointer;
}
button:focus-visible,
input:focus-visible,
select:focus-visible,
a:focus-visible {
  outline: 3px solid #77e3ff;
  outline-offset: 3px;
}
.skip {
  position: absolute;
  left: -999px;
  top: 0;
  padding: 0.6rem;
  background: #fff;
  color: #000;
}
.skip:focus {
  left: 0.75rem;
  z-index: 10;
}
.shell {
  max-width: 1440px;
  margin: auto;
  padding: 1.25rem;
}
.top {
  display: flex;
  gap: 1rem;
  align-items: flex-start;
  justify-content: space-between;
  padding-bottom: 1rem;
  border-bottom: 1px solid #33415f;
}
.eyebrow {
  margin: 0;
  color: #9eb1d7;
  font-size: 0.75rem;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}
.top h1 {
  margin: 0.15rem 0;
  font-size: clamp(1.45rem, 3vw, 2.35rem);
}
.capture,
.muted {
  color: #bdcbe7;
}
.capture {
  max-width: 38rem;
  margin: 0.4rem 0 0;
}
.banner {
  margin: 1rem 0;
  padding: 0.85rem 1rem;
  border-left: 4px solid #f6c85f;
  background: #332817;
  color: #fff0bf;
}
.banner.critical {
  border-left-color: #ff7b91;
  background: #421d2b;
  color: #ffe2e7;
}
.grid {
  display: grid;
  gap: 1rem;
}
.metrics {
  grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr));
  margin: 1rem 0;
}
.card,
.panel {
  min-width: 0;
  padding: 1rem;
  border: 1px solid #33415f;
  border-radius: 0.75rem;
  background: rgba(18, 27, 50, 0.9);
}
.metric strong {
  display: block;
  font-size: 1.65rem;
}
.metric span,
.muted {
  font-size: 0.88rem;
}
.attention {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(15rem, 1fr));
  gap: 0.6rem;
  margin: 1rem 0;
}
.attention-item {
  padding: 0.75rem;
  border-left: 4px solid #ff8d75;
  background: #382132;
}
.attention-item p {
  margin: 0.25rem 0 0;
}
.toolbar,
.pager,
.command-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.75rem;
  align-items: end;
}
.toolbar,
.pager {
  margin: 1rem 0;
}
.field {
  display: grid;
  min-width: 10rem;
  gap: 0.25rem;
}
.field input,
.field select {
  max-width: 100%;
  padding: 0.5rem;
  border: 1px solid #536587;
  border-radius: 0.35rem;
  background: #0d1529;
  color: #eef4ff;
}
.table-wrap {
  overflow-x: auto;
  border: 1px solid #33415f;
  border-radius: 0.75rem;
  background: #111a31;
}
table {
  width: 100%;
  min-width: 45rem;
  border-collapse: collapse;
}
caption {
  padding: 1rem;
  font-weight: 700;
  text-align: left;
}
th,
td {
  padding: 0.7rem;
  border-top: 1px solid #2c3956;
  text-align: left;
  vertical-align: top;
  overflow-wrap: anywhere;
}
th {
  color: #cbd8f2;
  font-size: 0.85rem;
}
.link {
  padding: 0;
  border: 0;
  background: none;
  color: #77e3ff;
  font-weight: 650;
  text-align: left;
  text-decoration: underline;
}
.button {
  padding: 0.45rem 0.7rem;
  border: 1px solid #6379a6;
  border-radius: 0.35rem;
  background: #263b66;
  color: #fff;
}
.button:disabled {
  cursor: not-allowed;
  opacity: 0.45;
}
.path {
  overflow-wrap: anywhere;
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
}
.facts,
.commands {
  display: grid;
  gap: 0.75rem;
}
.facts {
  grid-template-columns: repeat(auto-fit, minmax(13rem, 1fr));
}
.facts dt {
  color: #aebddb;
  font-size: 0.82rem;
}
.facts dd {
  margin: 0.15rem 0 0;
  overflow-wrap: anywhere;
}
.command {
  padding: 0.75rem;
  border: 1px solid #3d5078;
  border-radius: 0.5rem;
}
.command pre {
  margin: 0.5rem 0;
  padding: 0.65rem;
  overflow-wrap: anywhere;
  border-radius: 0.3rem;
  background: #080d1a;
  white-space: pre-wrap;
}
.notice {
  padding: 0.75rem;
  border-left: 4px solid #77e3ff;
  background: #182742;
}
.empty {
  padding: 1.5rem;
  color: #bdcbe7;
  text-align: center;
}
.footer {
  padding: 1.5rem 0;
  color: #aebddb;
  font-size: 0.82rem;
}
.back {
  margin: 0 0 1rem;
}
.sr-status {
  min-height: 1.4rem;
}
@media (max-width: 640px) {
  .shell {
    padding: 0.8rem;
  }
  .top {
    display: block;
  }
  .toolbar {
    display: grid;
    grid-template-columns: 1fr;
  }
  .field {
    min-width: 0;
  }
  .button {
    min-height: 2.6rem;
  }
  table {
    min-width: 37rem;
  }
  .facts,
  .attention {
    grid-template-columns: 1fr;
  }
}
"""


JAVASCRIPT = r"""
(() => {
  'use strict';
  const validPage = (value, fallback) =>
    Number.isInteger(value) && value >= 1 && value <= 100 ? value : fallback;
  const compare = (a, b) => (String(a) < String(b) ? -1 : String(a) > String(b) ? 1 : 0);
  const distances = (adjacency, anchor) => {
    const found = new Map();
    const pending = (adjacency.get(anchor) || []).map((id) => [id, 1]);
    let head = 0;
    while (head < pending.length) {
      const [id, distance] = pending[head++];
      if (found.has(id)) continue;
      found.set(id, distance);
      for (const related of adjacency.get(id) || []) pending.push([related, distance + 1]);
    }
    return found;
  };
  const dependencyImpact = (goal, workUnitId, direction = 'both', limit = 20, offset = 0) => {
    if (!goal || !goal.completeness || goal.completeness.graph_complete !== true)
      return {
        available: false,
        reason: 'Graph data is incomplete; structural dependency facts are unavailable.',
      };
    if (!['both', 'prerequisites', 'dependents'].includes(direction))
      return { available: false, reason: 'Unknown impact direction.' };
    if (!Number.isInteger(offset) || offset < 0)
      return { available: false, reason: 'Invalid relation offset.' };
    limit = validPage(limit, 20);
    const units = Array.isArray(goal.work_units) ? goal.work_units : [];
    const byId = new Map(units.map((unit) => [unit.id, unit]));
    const anchor = byId.get(workUnitId);
    if (!anchor) return { available: false, reason: 'The selected work unit is not captured.' };
    const prerequisites = new Map();
    const dependents = new Map();
    for (const unit of units) {
      prerequisites.set(
        unit.id,
        Array.isArray(unit.prerequisite_ids) ? unit.prerequisite_ids.slice() : [],
      );
      dependents.set(unit.id, []);
    }
    for (const [dependentId, ids] of prerequisites)
      for (const prerequisiteId of ids) {
        const list = dependents.get(prerequisiteId);
        if (list) list.push(dependentId);
      }
    for (const ids of dependents.values()) ids.sort(compare);
    const prerequisiteDistances = distances(prerequisites, workUnitId);
    const dependentDistances = distances(dependents, workUnitId);
    const clearsGate = new Set();
    if (anchor.status !== 'complete')
      for (const dependentId of dependents.get(workUnitId) || []) {
        const dependent = byId.get(dependentId);
        if (
          dependent &&
          !dependent.structural_ready &&
          (prerequisites.get(dependentId) || []).every(
            (id) => id === workUnitId || (byId.get(id) || {}).status === 'complete',
          )
        )
          clearsGate.add(dependentId);
      }
    const row = (id, relation, distance) => {
      const unit = byId.get(id);
      return {
        work_unit_id: id,
        relation,
        distance,
        direct: distance === 1,
        status: unit.status,
        checkpoint_id: unit.checkpoint_id,
        structural_ready: unit.structural_ready,
        incomplete_blocker: relation === 'prerequisite' && unit.status !== 'complete',
        would_clear_direct_prerequisite_gate: relation === 'dependent' && clearsGate.has(id),
      };
    };
    const sorted = (items, relation) =>
      Array.from(items, ([id, distance]) => row(id, relation, distance)).sort(
        (a, b) => a.distance - b.distance || compare(a.work_unit_id, b.work_unit_id),
      );
    const prerequisiteRows = sorted(prerequisiteDistances, 'prerequisite');
    const dependentRows = sorted(dependentDistances, 'dependent');
    const relations =
      direction === 'prerequisites'
        ? prerequisiteRows
        : direction === 'dependents'
          ? dependentRows
          : prerequisiteRows.concat(dependentRows);
    const direct = prerequisites.get(workUnitId) || [];
    const summary = {
      direct_prerequisites_total: direct.length,
      all_prerequisites_total: prerequisiteDistances.size,
      incomplete_blockers_total: prerequisiteRows.filter((item) => item.incomplete_blocker).length,
      direct_dependents_total: (dependents.get(workUnitId) || []).length,
      all_dependents_total: dependentDistances.size,
      direct_prerequisite_gates_cleared_if_completed: clearsGate.size,
    };
    const page = relations.slice(offset, offset + limit);
    return {
      available: true,
      anchor: {
        work_unit_id: workUnitId,
        status: anchor.status,
        checkpoint_id: anchor.checkpoint_id,
        structural_ready: anchor.structural_ready,
        direct_prerequisites_total: direct.length,
        incomplete_direct_prerequisites_total: direct.filter(
          (id) => (byId.get(id) || {}).status !== 'complete',
        ).length,
      },
      summary,
      direction,
      relations: page,
      total: relations.length,
      limit,
      offset,
      next_offset: offset + page.length < relations.length ? offset + page.length : null,
    };
  };
  const quote = (value, shell) => {
    const text = String(value);
    return shell === 'powershell'
      ? "'" + text.replace(/'/g, "''") + "'"
      : "'" + text.replace(/'/g, "'\\''") + "'";
  };
  const goalViewModel = (goal) => {
    const progress = goal.progress || {};
    const acceptance = progress.acceptance || {};
    const criteria = acceptance.criteria == null ? 'Unavailable' : acceptance.criteria;
    return {
      work: progress.work || {},
      acceptance,
      acceptanceSummary: (acceptance.evidence || 0) + ' evidence / ' + criteria + ' criteria',
      budget: goal.budget || { present: false },
      completeness: goal.completeness || {},
      dependencies: Array.isArray(goal.dependencies) ? goal.dependencies : [],
      checkpoints: Array.isArray(goal.checkpoints) ? goal.checkpoints : [],
      providerEffects: goal.provider_effects || {},
      units: Array.isArray(goal.work_units) ? goal.work_units : [],
    };
  };
  const goalWorkPage = (goal, query = '', status = '', page = 0, limit = 20) => {
    const model = goalViewModel(goal);
    const needle = String(query).toLocaleLowerCase();
    const rows = model.units.filter(
      (unit) =>
        (!needle || (unit.id + ' ' + unit.title).toLocaleLowerCase().includes(needle)) &&
        (!status || unit.status === status),
    );
    const size = validPage(limit, 20);
    const offset = Math.max(0, Number.isInteger(page) ? page : 0) * size;
    return { total: rows.length, rows: rows.slice(offset, offset + size), limit: size, offset };
  };
  const expandGuidance = (snapshot, templateId, values = {}) => {
    const template =
      snapshot && snapshot.guidance_templates && snapshot.guidance_templates[templateId];
    const context = snapshot && snapshot.guidance_context;
    if (
      !template ||
      template.read_only !== true ||
      !context ||
      typeof context.cwd !== 'string' ||
      !context.cwd ||
      !Array.isArray(context.argv_prefix) ||
      !Array.isArray(template.argv_suffix)
    )
      return null;
    const allowed = new Set([
      'goal-overview',
      'goal-status',
      'work-dependencies',
      'work-impact',
      'doctor',
    ]);
    if (!allowed.has(templateId)) return null;
    const replace = (token) => {
      if (typeof token !== 'string') return null;
      const match = token.match(/^\{(root|goal_id|work_unit_id|limit|offset)\}$/);
      if (!match) return token;
      const name = match[1];
      const value = name === 'root' ? snapshot.project && snapshot.project.root : values[name];
      if (name === 'limit' || name === 'offset') {
        if (
          !Number.isInteger(value) ||
          value < 0 ||
          (name === 'limit' && (value < 1 || value > 100))
        )
          return null;
      } else if (typeof value !== 'string' || !value) return null;
      return String(value);
    };
    const suffix = template.argv_suffix.map(replace);
    if (suffix.some((value) => value === null)) return null;
    const argv = context.argv_prefix.concat(suffix);
    const shell = context.shell === 'powershell' ? 'powershell' : 'posix';
    const environment = context.env && typeof context.env === 'object' ? context.env : {};
    const keys = Object.keys(environment).sort(compare);
    if (keys.some((key) => key !== 'PYTHONPATH' || typeof environment[key] !== 'string'))
      return null;
    const invocation = argv.map((value) => quote(value, shell)).join(' ');
    const commandText =
      shell === 'powershell'
        ? 'Set-Location -LiteralPath ' +
          quote(context.cwd, shell) +
          '; ' +
          keys.map((key) => '$env:' + key + '=' + quote(environment[key], shell) + '; ').join('') +
          '& ' +
          invocation
        : 'cd -- ' +
          quote(context.cwd, shell) +
          ' && ' +
          keys.map((key) => key + '=' + quote(environment[key], shell) + ' ').join('') +
          invocation;
    return {
      label: template.label,
      read_only: true,
      cwd: context.cwd,
      env: environment,
      argv,
      command_text: commandText,
    };
  };
  window.TasktraCockpit = {
    compare,
    dependencyImpact,
    expandGuidance,
    goalViewModel,
    goalWorkPage,
    quote,
  };
  if (typeof document === 'undefined') return;
  const snapshotNode = document.getElementById('tasktra-snapshot');
  if (!snapshotNode) return;
  let snapshot;
  try {
    snapshot = JSON.parse(snapshotNode.textContent);
  } catch (_) {
    return;
  }
  const root = document.getElementById('cockpit');
  const live = document.getElementById('route-status');
  let routeFocusPending = false;
  const el = (tag, text, attrs = {}) => {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    for (const [key, value] of Object.entries(attrs)) {
      if (key === 'class') node.className = value;
      else if (key === 'type') node.type = value;
      else node.setAttribute(key, value);
    }
    if (tag === 'input' && attrs.type === 'search')
      node.dataset.filter = attrs['data-filter'] || 'query';
    return node;
  };
  const clear = (node) => {
    while (node.firstChild) node.removeChild(node.firstChild);
  };
  const hash = () =>
    new URLSearchParams(location.hash.startsWith('#') ? location.hash.slice(1) : '');
  const state = () => {
    const params = hash();
    return {
      view: params.get('view') || 'portfolio',
      goal: params.get('goal') || '',
      unit: params.get('unit') || '',
      q: params.get('q') || '',
      status: params.get('status') || '',
      attention: params.get('attention') || '',
      page: Math.max(0, Number.parseInt(params.get('page') || '0', 10) || 0),
      unitPage: Math.max(0, Number.parseInt(params.get('unitPage') || '0', 10) || 0),
      unitQ: params.get('unitQ') || '',
      unitStatus: params.get('unitStatus') || '',
      relationPage: Math.max(0, Number.parseInt(params.get('relationPage') || '0', 10) || 0),
    };
  };
  const navigate = (changes, replace = false) => {
    const next = Object.assign({}, state(), changes);
    const params = new URLSearchParams();
    for (const key of [
      'view',
      'goal',
      'unit',
      'q',
      'status',
      'attention',
      'page',
      'unitPage',
      'unitQ',
      'unitStatus',
      'relationPage',
    ]) {
      if (next[key] !== '' && next[key] !== 0 && next[key] !== 'portfolio')
        params.set(key, String(next[key]));
    }
    if (
      Object.prototype.hasOwnProperty.call(changes, 'q') ||
      Object.prototype.hasOwnProperty.call(changes, 'unitQ')
    )
      replace = true;
    if (replace) {
      history.replaceState(null, '', location.pathname + location.search + '#' + params.toString());
      render();
    } else location.hash = params.toString();
  };
  const button = (text, action, className = 'button') => {
    const node = el('button', text, { type: 'button', class: className });
    node.addEventListener('click', action);
    return node;
  };
  const badge = (text, kind = '') => el('span', text, { class: 'pill ' + kind });
  const details = (pairs) => {
    const list = el('dl', undefined, { class: 'facts' });
    for (const [name, value] of pairs) {
      list.append(el('div'));
      const holder = list.lastChild;
      holder.append(
        el('dt', name),
        el('dd', value === null || value === undefined ? 'Unavailable' : value),
      );
    }
    return list;
  };
  const pageSize = () => validPage(snapshot.bounds && snapshot.bounds.page_size, 20);
  const captureBanner = () => {
    const banner = el(
      'aside',
      'Captured ' +
        ((snapshot.capture || {}).captured_at || 'at an unavailable time') +
        '. Static capture; regenerate to refresh. This local file includes sensitive project names, paths, statuses, and structural relationships.',
      { class: 'banner', role: 'note' },
    );
    return banner;
  };
  const header = (title, description) => {
    const section = el('section');
    section.append(
      el('p', 'Offline operator cockpit', { class: 'eyebrow' }),
      el('h2', title),
      el('p', description, { class: 'muted' }),
    );
    return section;
  };
  const addMetric = (box, value, label) => {
    const item = el('div', undefined, { class: 'card metric' });
    item.append(el('strong', value), el('span', label));
    box.append(item);
  };
  const addCommands = (container, ids, values) => {
    const wrap = el('section', undefined, { class: 'panel commands' });
    wrap.append(
      el('h3', 'Read-only CLI guidance'),
      el('p', 'Commands use the captured Python and source binding. They inspect state only.', {
        class: 'muted',
      }),
    );
    for (const id of ids) {
      const command = expandGuidance(snapshot, id, values);
      if (!command) continue;
      const block = el('article', undefined, { class: 'command' });
      block.append(
        el('strong', command.label),
        el('div', 'cwd: ' + command.cwd, { class: 'muted path' }),
      );
      const code = el('pre', command.command_text, { tabindex: '0' });
      const status = el('span', '', { class: 'muted' });
      const copy = button('Copy command', async () => {
        try {
          if (!navigator.clipboard || !navigator.clipboard.writeText)
            throw new Error('clipboard unavailable');
          await navigator.clipboard.writeText(command.command_text);
          status.textContent = 'Copied.';
        } catch (_) {
          const selection = window.getSelection();
          selection.removeAllRanges();
          const range = document.createRange();
          range.selectNodeContents(code);
          selection.addRange(range);
          code.focus();
          status.textContent = 'Command selected; copy it from the visible text.';
        }
      });
      const actions = el('div', undefined, { class: 'command-actions' });
      actions.append(copy, status);
      block.append(code, actions);
      wrap.append(block);
    }
    container.append(wrap);
  };
  const pagination = (container, page, total, onPage) => {
    const pages = Math.max(1, Math.ceil(total / pageSize()));
    const controls = el('nav', undefined, { class: 'pager', 'aria-label': 'Pagination' });
    controls.append(
      button('Previous', () => onPage(page - 1), 'button'),
      el('span', 'Page ' + (page + 1) + ' of ' + pages, { class: 'muted' }),
      button('Next', () => onPage(page + 1), 'button'),
    );
    controls.firstChild.disabled = page <= 0;
    controls.lastChild.disabled = page >= pages - 1;
    container.append(controls);
  };
  const portfolio = (current) => {
    const section = header('Portfolio', 'Attention-first view of one verified ledger capture.');
    const capture = snapshot.capture || {};
    const source = snapshot.source_provenance || {};
    section.append(
      details([
        ['Project', snapshot.project && snapshot.project.name],
        ['Root', snapshot.project && snapshot.project.root],
        ['Captured', capture.captured_at],
        ['Schema', capture.schema_version],
        ['Audit sequence', capture.audit_sequence],
        ['Audit head', capture.audit_head_sha256],
        ['State manifest', capture.state_manifest_sha256],
        [
          'Source',
          source.foreign_source_checkout
            ? 'Foreign source checkout'
            : source.source_matches_project
              ? 'Source matches project'
              : source.package_kind,
        ],
      ]),
    );
    if (source.foreign_source_checkout)
      section.append(
        el(
          'p',
          'The captured Tasktra source is foreign to this project. Guidance remains bound to that captured source.',
          { class: 'banner critical' },
        ),
      );
    const stop = snapshot.runtime && snapshot.runtime.emergency_stop;
    if (stop && stop.active)
      section.append(
        el('p', 'Emergency stop active' + (stop.reason ? ': ' + stop.reason : '.'), {
          class: 'banner critical',
        }),
      );
    const metrics = el('section', undefined, { class: 'grid metrics' });
    const aggregates = snapshot.aggregates || {};
    addMetric(metrics, (aggregates.goals || {}).total || 0, 'Goals');
    addMetric(metrics, (aggregates.work_units || {}).total || 0, 'Work units');
    addMetric(metrics, (aggregates.leases || {}).live || 0, 'Live leases');
    addMetric(metrics, (aggregates.leases || {}).stored_leased || 0, 'Stored leases');
    addMetric(metrics, (aggregates.leases || {}).expired || 0, 'Expired leases');
    addMetric(metrics, (aggregates.budgets || {}).exhausted_goals || 0, 'Exhausted budgets');
    addMetric(
      metrics,
      Object.entries(aggregates.provider_effects || {})
        .sort(([left], [right]) => compare(left, right))
        .map(([status, count]) => status + ': ' + count)
        .join(', ') || 'None',
      'Provider effects',
    );
    section.append(metrics);
    const allAttention = (snapshot.goals || [])
      .filter((goal) => Array.isArray(goal.attention) && goal.attention.length)
      .slice(0, pageSize());
    if (allAttention.length) {
      const attention = el('section', undefined, {
        class: 'attention',
        'aria-label': 'Captured attention',
      });
      attention.append(
        el('h3', 'Captured attention'),
        el(
          'p',
          'Showing the first ' +
            allAttention.length +
            ' attention goals. Use the filters below to narrow the full captured portfolio.',
          { class: 'muted' },
        ),
      );
      for (const goal of allAttention)
        for (const item of goal.attention) {
          const card = el('article', undefined, { class: 'attention-item' });
          const go = button(
            goal.id + ': ' + item.code + (item.count === undefined ? '' : ' (' + item.count + ')'),
            () => navigate({ view: 'goal', goal: goal.id, unit: '', page: 0 }),
            'link',
          );
          card.append(go, el('p', item.detail));
          attention.append(card);
        }
      section.append(attention);
    }
    const toolbar = el('form', undefined, { class: 'toolbar', 'aria-label': 'Portfolio filters' });
    toolbar.addEventListener('submit', (event) => event.preventDefault());
    const textInput = el('input', undefined, {
      type: 'search',
      value: current.q,
      placeholder: 'Title or identifier',
    });
    const statusSelect = el('select');
    statusSelect.append(el('option', 'All statuses', { value: '' }));
    const attentionSelect = el('select');
    attentionSelect.append(el('option', 'All attention', { value: '' }));
    const statuses = Array.from(new Set((snapshot.goals || []).map((goal) => goal.status))).sort(
      compare,
    );
    const codes = Array.from(
      new Set(
        (snapshot.goals || []).flatMap((goal) => (goal.attention || []).map((item) => item.code)),
      ),
    ).sort(compare);
    for (const value of statuses) statusSelect.append(el('option', value, { value }));
    for (const value of codes) attentionSelect.append(el('option', value, { value }));
    statusSelect.value = current.status;
    attentionSelect.value = current.attention;
    const update = () =>
      navigate({
        q: textInput.value,
        status: statusSelect.value,
        attention: attentionSelect.value,
        page: 0,
      });
    textInput.addEventListener('input', update);
    statusSelect.addEventListener('change', update);
    attentionSelect.addEventListener('change', update);
    const labelled = (label, input) => {
      const field = el('label', undefined, { class: 'field' });
      field.append(el('span', label), input);
      return field;
    };
    toolbar.append(
      labelled('Filter goals', textInput),
      labelled('Status', statusSelect),
      labelled('Attention', attentionSelect),
    );
    section.append(toolbar);
    const needle = current.q.toLocaleLowerCase();
    const goals = (snapshot.goals || []).filter(
      (goal) =>
        (!needle || (goal.id + ' ' + goal.title).toLocaleLowerCase().includes(needle)) &&
        (!current.status || goal.status === current.status) &&
        (!current.attention ||
          (goal.attention || []).some((item) => item.code === current.attention)),
    );
    const start = current.page * pageSize();
    const visible = goals.slice(start, start + pageSize());
    const tableWrap = el('div', undefined, { class: 'table-wrap' });
    const table = el('table');
    table.append(
      el(
        'caption',
        'Goals: ' +
          goals.length +
          ' matching, ' +
          (snapshot.completeness || {}).goals_captured +
          ' captured of ' +
          (snapshot.completeness || {}).goals_total +
          '.',
      ),
    );
    const head = el('thead');
    const headRow = el('tr');
    for (const name of ['Goal', 'Status', 'Progress', 'Attention', 'Graph'])
      headRow.append(el('th', name, { scope: 'col' }));
    head.append(headRow);
    table.append(head);
    const body = el('tbody');
    for (const goal of visible) {
      const row = el('tr');
      const cell = el('td');
      cell.append(
        button(
          goal.title || goal.id,
          () => navigate({ view: 'goal', goal: goal.id, unit: '', page: 0 }),
          'link',
        ),
        el('div', goal.id, { class: 'muted path' }),
      );
      row.append(
        cell,
        el('td', goal.status),
        el(
          'td',
          ((goal.progress || {}).work || {}).complete +
            ' / ' +
            ((goal.progress || {}).work || {}).total,
        ),
        el(
          'td',
          (goal.attention || [])
            .map((item) => item.code + (item.count === undefined ? '' : ' (' + item.count + ')'))
            .join(', ') || 'None',
        ),
        el(
          'td',
          goal.completeness && goal.completeness.graph_complete
            ? 'Complete capture'
            : 'Bounded / incomplete',
        ),
      );
      body.append(row);
    }
    if (!visible.length) {
      const row = el('tr');
      const cell = el('td', 'No captured goals match these filters.', { class: 'empty' });
      cell.colSpan = 5;
      row.append(cell);
      body.append(row);
    }
    table.append(body);
    tableWrap.append(table);
    section.append(tableWrap);
    pagination(section, current.page, goals.length, (page) => navigate({ page }));
    const complete = snapshot.completeness || {};
    if (complete.normal_goals_omitted)
      section.append(
        el(
          'p',
          complete.normal_goals_omitted +
            ' normal goal summaries were omitted by the capture bound.',
          { class: 'banner' },
        ),
      );
    addCommands(section, ['doctor'], { limit: pageSize(), offset: 0 });
    return section;
  };
  const goalView = (current, goal) => {
    const section = header(
      goal.title || goal.id,
      'Goal detail from the captured portfolio snapshot.',
    );
    section.prepend(
      button('← Portfolio', () => navigate({ view: 'portfolio', goal: '', unit: '' }), 'back link'),
    );
    const model = goalViewModel(goal);
    const c = model.completeness;
    const work = model.work;
    const budget = model.budget;
    const token = budget.tokens || {};
    const attempts = budget.attempts || {};
    const elapsed = budget.elapsed_ms || {};
    const concurrency = budget.concurrency || {};
    const counts = (map) =>
      Object.entries(map || {})
        .sort(([left], [right]) => compare(left, right))
        .map(([name, count]) => name + ': ' + count)
        .join(', ') || 'None';
    const number = (value) => (value === null || value === undefined ? 'Unlimited' : value);
    section.append(
      details([
        ['Goal ID', goal.id],
        ['Status', goal.status],
        ['Priority', goal.priority],
        [
          'Work progress',
          (work.complete || 0) +
            ' complete / ' +
            (work.total || 0) +
            ' total (' +
            counts(work.by_status) +
            ')',
        ],
        ['Acceptance progress', model.acceptanceSummary],
        [
          'Token budget',
          budget.present
            ? 'total ' +
              number(token.total) +
              ', consumed ' +
              (token.consumed || 0) +
              ', reserved ' +
              (token.reserved || 0) +
              ', remaining ' +
              number(token.remaining)
            : 'Not configured',
        ],
        [
          'Attempt budget',
          budget.present
            ? 'total ' +
              number(attempts.total) +
              ', consumed ' +
              (attempts.consumed || 0) +
              ', remaining ' +
              number(attempts.remaining)
            : 'Not configured',
        ],
        [
          'Elapsed budget',
          budget.present
            ? 'total ' +
              number(elapsed.total) +
              ' ms, consumed ' +
              (elapsed.consumed || 0) +
              ' ms, reserved held ' +
              (elapsed.reserved_held || 0) +
              ' ms, remaining ' +
              number(elapsed.remaining) +
              ' ms'
            : 'Not configured',
        ],
        [
          'Concurrency budget',
          budget.present
            ? 'maximum ' +
              number(concurrency.maximum) +
              ', occupied ' +
              (concurrency.occupied || 0) +
              ', available ' +
              number(concurrency.available)
            : 'Not configured',
        ],
        ['Budget exhausted', budget.present && budget.exhausted ? 'Yes' : 'No'],
        [
          'Work units',
          c.work_units_captured +
            ' captured / ' +
            c.work_units_total +
            ' total / ' +
            c.work_units_omitted +
            ' omitted',
        ],
        [
          'Dependency edges',
          c.dependency_edges_captured +
            ' captured / ' +
            c.dependency_edges_total +
            ' total / ' +
            c.dependency_edges_omitted +
            ' omitted',
        ],
        ['Graph', c.graph_complete ? 'Complete' : 'Incomplete — derived facts unavailable'],
        [
          'Goal dependencies',
          model.dependencies.map((item) => item.id + ' (' + item.status + ')').join(', ') || 'None',
        ],
        [
          'Checkpoints',
          model.checkpoints
            .map(
              (item) =>
                item.id +
                ' #' +
                item.position +
                ' (' +
                item.status +
                (item.reached_at ? ', reached ' + item.reached_at : '') +
                ')',
            )
            .join('; ') || 'None',
        ],
        ['Provider effects', counts(model.providerEffects)],
        [
          'Intake',
          goal.intake && goal.intake.draining
            ? 'Draining'
            : goal.intake && goal.intake.accepting_claims
              ? 'Accepting claims'
              : 'Not accepting claims',
        ],
        [
          'Stored leases',
          (goal.leases || {}).stored_leased +
            ', live ' +
            (goal.leases || {}).live +
            ', expired ' +
            (goal.leases || {}).expired,
        ],
      ]),
    );
    if (!c.graph_complete)
      section.append(
        el(
          'p',
          'This bounded capture omitted graph detail. Structural readiness, dependencies, distances, blockers, and gate-clear counts are unavailable rather than zero.',
          { class: 'banner' },
        ),
      );
    if (c.work_units_omitted || c.dependency_edges_omitted)
      section.append(
        el(
          'p',
          'Bounded detail: ' +
            c.work_units_captured +
            ' of ' +
            c.work_units_total +
            ' work units and ' +
            c.dependency_edges_captured +
            ' of ' +
            c.dependency_edges_total +
            ' dependency edges were captured.',
          { class: 'banner' },
        ),
      );
    if ((goal.attention || []).length) {
      const attention = el('section', undefined, { class: 'attention' });
      attention.append(el('h3', 'Attention'));
      for (const item of goal.attention)
        attention.append(
          el(
            'article',
            item.code +
              (item.count === undefined ? '' : ' (' + item.count + ')') +
              ': ' +
              item.detail,
            { class: 'attention-item' },
          ),
        );
      section.append(attention);
    }
    const toolbar = el('form', undefined, { class: 'toolbar', 'aria-label': 'Work unit filters' });
    toolbar.addEventListener('submit', (event) => event.preventDefault());
    const textInput = el('input', undefined, {
      type: 'search',
      value: current.unitQ,
      placeholder: 'Work unit title or identifier',
      'data-filter': 'unit-query',
    });
    const statusSelect = el('select');
    statusSelect.append(el('option', 'All work statuses', { value: '' }));
    for (const value of Array.from(new Set(model.units.map((unit) => unit.status))).sort(compare))
      statusSelect.append(el('option', value, { value }));
    statusSelect.value = current.unitStatus;
    const update = () =>
      navigate({ unitQ: textInput.value, unitStatus: statusSelect.value, unitPage: 0 });
    textInput.addEventListener('input', update);
    statusSelect.addEventListener('change', update);
    const labelled = (label, input) => {
      const field = el('label', undefined, { class: 'field' });
      field.append(el('span', label), input);
      return field;
    };
    toolbar.append(labelled('Filter work units', textInput), labelled('Status', statusSelect));
    section.append(toolbar);
    const unitPage = goalWorkPage(
      goal,
      current.unitQ,
      current.unitStatus,
      current.unitPage,
      pageSize(),
    );
    const units = unitPage.total;
    const visible = unitPage.rows;
    const tableWrap = el('div', undefined, { class: 'table-wrap' });
    const table = el('table');
    table.append(
      el('caption', 'Work units: ' + units + ' matching, ' + model.units.length + ' captured.'),
    );
    const thead = el('thead');
    const tr = el('tr');
    for (const name of [
      'Work unit',
      'Status',
      'Checkpoint',
      'Attempts and outcome',
      'Lease',
      'Graph state',
    ])
      tr.append(el('th', name, { scope: 'col' }));
    thead.append(tr);
    table.append(thead);
    const body = el('tbody');
    for (const unit of visible) {
      const row = el('tr');
      const label = el('td');
      label.append(
        button(
          unit.title || unit.id,
          () => navigate({ view: 'unit', goal: goal.id, unit: unit.id, relationPage: 0 }),
          'link',
        ),
        el('div', unit.id, { class: 'muted path' }),
      );
      row.append(
        label,
        el('td', unit.status),
        el('td', unit.checkpoint_id || 'None'),
        el(
          'td',
          'Attempts: ' +
            unit.attempt_count +
            '; last outcome: ' +
            (unit.last_outcome_class || 'None'),
        ),
        el(
          'td',
          unit.lease ? unit.lease.state + ' until ' + unit.lease.expires_at : 'No stored lease',
        ),
        el(
          'td',
          c.graph_complete
            ? unit.structural_ready
              ? 'Structurally ready'
              : 'Structural gates remain'
            : 'Unavailable',
        ),
      );
      body.append(row);
    }
    table.append(body);
    tableWrap.append(table);
    section.append(tableWrap);
    pagination(section, current.unitPage, units, (unitPage) => navigate({ unitPage }));
    addCommands(section, ['goal-overview', 'goal-status'], {
      goal_id: goal.id,
      limit: pageSize(),
      offset: 0,
    });
    return section;
  };
  const unitView = (current, goal, unit) => {
    const section = header(
      unit.title || unit.id,
      'Captured work-unit status and bounded structural impact.',
    );
    section.prepend(
      button(
        '← ' + (goal.title || goal.id),
        () => navigate({ view: 'goal', goal: goal.id, unit: '', relationPage: 0 }),
        'back link',
      ),
    );
    const c = goal.completeness || {};
    section.append(
      details([
        ['Work unit ID', unit.id],
        ['Stored status', unit.status],
        ['Checkpoint', unit.checkpoint_id || 'None'],
        ['Attempts', unit.attempt_count],
        ['Last outcome', unit.last_outcome_class || 'None'],
        ['Lease', unit.lease ? unit.lease.state + ' until ' + unit.lease.expires_at : 'None'],
        ['Updated', unit.updated_at],
      ]),
    );
    if (!c.graph_complete) {
      section.append(
        el(
          'p',
          'This goal has an incomplete captured graph. Dependency, readiness, blocker, distance, and impact facts are unavailable.',
          { class: 'banner' },
        ),
      );
      addCommands(section, ['work-dependencies', 'work-impact'], {
        goal_id: goal.id,
        work_unit_id: unit.id,
        limit: pageSize(),
        offset: 0,
      });
      return section;
    }
    const report = dependencyImpact(
      goal,
      unit.id,
      'both',
      pageSize(),
      current.relationPage * pageSize(),
    );
    section.append(
      el(
        'p',
        'Structural impact only. Completing a prerequisite does not by itself authorize a claim; tasktra work explain rechecks all claim gates.',
        { class: 'notice' },
      ),
    );
    section.append(
      el(
        'p',
        'Full claim eligibility was not evaluated. It requires actor, current envelope digest, requested lease duration, token reservation, and approval context through tasktra work explain.',
        { class: 'muted' },
      ),
    );
    if (!report.available) section.append(el('p', report.reason, { class: 'banner' }));
    else {
      section.append(
        details([
          ['Structural ready', report.anchor.structural_ready ? 'Yes' : 'No'],
          ['Direct prerequisites', report.summary.direct_prerequisites_total],
          ['All prerequisites', report.summary.all_prerequisites_total],
          ['Incomplete blockers', report.summary.incomplete_blockers_total],
          ['Direct dependents', report.summary.direct_dependents_total],
          ['All dependents', report.summary.all_dependents_total],
          [
            'Direct gates cleared if completed',
            report.summary.direct_prerequisite_gates_cleared_if_completed,
          ],
        ]),
      );
      const wrap = el('div', undefined, { class: 'table-wrap' });
      const table = el('table');
      table.append(el('caption', 'Dependency impact relations'));
      const head = el('thead');
      const tr = el('tr');
      for (const name of ['Relation', 'Work unit', 'Distance', 'Status', 'Structural note'])
        tr.append(el('th', name, { scope: 'col' }));
      head.append(tr);
      table.append(head);
      const body = el('tbody');
      for (const item of report.relations) {
        const row = el('tr');
        const note = item.incomplete_blocker
          ? 'Incomplete prerequisite blocker'
          : item.would_clear_direct_prerequisite_gate
            ? 'Direct gate would clear if anchor completes'
            : item.direct
              ? 'Direct relation'
              : '';
        row.append(
          el('td', item.relation),
          el('td', item.work_unit_id, { class: 'path' }),
          el('td', item.distance),
          el('td', item.status),
          el('td', note),
        );
        body.append(row);
      }
      if (!report.relations.length) {
        const row = el('tr');
        const cell = el('td', 'No captured structural relations.', { class: 'empty' });
        cell.colSpan = 5;
        row.append(cell);
        body.append(row);
      }
      table.append(body);
      wrap.append(table);
      section.append(wrap);
      const pager = el('nav', undefined, {
        class: 'pager',
        'aria-label': 'Impact relation pagination',
      });
      pager.append(
        button('Previous', () => navigate({ relationPage: current.relationPage - 1 }), 'button'),
        el('span', 'Page ' + (current.relationPage + 1), { class: 'muted' }),
        button('Next', () => navigate({ relationPage: current.relationPage + 1 }), 'button'),
      );
      pager.firstChild.disabled = current.relationPage <= 0;
      pager.lastChild.disabled = report.next_offset === null;
      section.append(pager);
    }
    addCommands(section, ['work-dependencies', 'work-impact'], {
      goal_id: goal.id,
      work_unit_id: unit.id,
      limit: pageSize(),
      offset: current.relationPage * pageSize(),
    });
    return section;
  };
  const render = () => {
    const active = document.activeElement;
    const filter = active && active.dataset ? active.dataset.filter : '';
    const cursor =
      active && typeof active.selectionStart === 'number' ? active.selectionStart : null;
    const current = state();
    clear(root);
    let page;
    let routeLabel = 'Portfolio view';
    const goal = (snapshot.goals || []).find((item) => item.id === current.goal);
    const unit = goal && (goal.work_units || []).find((item) => item.id === current.unit);
    if (current.view === 'goal' && goal) {
      page = goalView(current, goal);
      routeLabel = 'Goal view: ' + goal.id;
    } else if (current.view === 'unit' && goal && unit) {
      page = unitView(current, goal, unit);
      routeLabel = 'Work unit view: ' + unit.id;
    } else page = portfolio(current);
    root.append(page);
    live.textContent = routeLabel;
    if (filter) {
      const replacement = root.querySelector('[data-filter="' + filter + '"]');
      if (replacement) {
        replacement.focus();
        if (cursor !== null && typeof replacement.setSelectionRange === 'function')
          replacement.setSelectionRange(cursor, cursor);
      }
    } else if (routeFocusPending) {
      root.focus();
    }
    routeFocusPending = false;
  };
  document.getElementById('freshness').append(captureBanner());
  window.addEventListener('hashchange', () => {
    routeFocusPending = true;
    render();
  });
  render();
})();
"""


def _safe_json(snapshot: Mapping[str, Any]) -> str:
    """Serialize snapshot data so it cannot terminate the data script element."""
    try:
        encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise ValueError("cockpit snapshot must be JSON serializable") from error
    return (
        encoded.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _source_hash(source: str) -> str:
    return b64encode(sha256(source.encode("utf-8")).digest()).decode("ascii")


def render(snapshot: Mapping[str, Any]) -> bytes:
    """Render one validated cockpit snapshot without observing external state."""
    if not isinstance(snapshot, Mapping):
        raise ValueError("cockpit snapshot must be a mapping")
    if snapshot.get("kind") != "tasktra.operator-cockpit.snapshot" or snapshot.get("version") != 1:
        raise ValueError("unsupported cockpit snapshot version")
    if snapshot.get("read_only") is not True or snapshot.get("claimability_evaluated") is not False:
        raise ValueError("cockpit snapshot must be read-only and not evaluate claimability")
    payload = _safe_json(snapshot)
    csp = (
        "default-src 'none'; script-src 'sha256-" + _source_hash(JAVASCRIPT) + "'; "
        "style-src 'sha256-" + _source_hash(CSS) + "'; img-src data:; connect-src 'none'; "
        "font-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'"
    )
    html = (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "<meta http-equiv=\"Content-Security-Policy\" content=\"" + csp + "\">\n"
        "<meta name=\"referrer\" content=\"no-referrer\">\n<title>Tasktra operator cockpit</title>\n<style>"
        + CSS + "</style>\n</head>\n<body>\n<a class=\"skip\" href=\"#cockpit\">Skip to cockpit</a>\n"
        "<div class=\"shell\"><header class=\"top\"><div><p class=\"eyebrow\">Tasktra</p><h1>Operator cockpit</h1>"
        "<p class=\"capture\">Read-only offline ledger capture. No Tasktra lifecycle controls are available here.</p></div></header>"
        "<div id=\"freshness\"></div><p id=\"route-status\" class=\"sr-status\" aria-live=\"polite\"></p>"
        "<main id=\"cockpit\" tabindex=\"-1\"></main><footer class=\"footer\">Static local capture. Inspect commands are read-only; this file cannot claim, approve, dispatch, recover, or change Tasktra state.</footer></div>\n"
        "<script id=\"tasktra-snapshot\" type=\"application/json\">" + payload + "</script>\n<script>" + JAVASCRIPT + "</script>\n</body>\n</html>\n"
    )
    return html.encode("utf-8")
