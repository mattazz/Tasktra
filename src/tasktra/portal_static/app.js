(() => {
  "use strict";

  const POLL_MS = 3000;
  const MAX_BACKOFF_MS = 30000;
  const state = {
    snapshot: null,
    demo: false,
    live: true,
    motion: true,
    selectedGoalId: null,
    selected: null,
    activity: { workId: null, agent: null, data: null, controller: null, timer: null, sequence: 0, follow: true, kind: "all", renderedKind: null, feedSignature: null, error: "" },
    analytics: { model: "", role: "", since: "" },
    agentFilters: { query: "", role: "", model: "", state: "", sort: "recent" },
    requestSequence: 0,
    activeView: "overview",
    requestInFlight: false,
    retryMs: POLL_MS,
    timer: null,
    controller: null,
    lastError: null,
    modeGeneration: 0,
    liveSnapshot: null,
    relationshipMap: null,
    relationshipMapScopeKey: null,
    relationshipMapMode: null,
    portalInsights: null,
    portalTimeline: null,
    portalOutcomes: null,
    workspace: { restoreGeneration: 0, pending: false, requested: null, projectKey: null, liveProjectKey: null, mode: "live", views: [], selectedViewId: "", notice: "", hashHandled: false, comparisonWorkIds: [], comparisonSignature: null },
  };

  const $ = (id) => document.getElementById(id);
  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  };
  const list = (tag, className, items) => {
    const node = el(tag, className);
    items.forEach((item) => node.append(item));
    return node;
  };
  const safeClass = (value) =>
    String(value || "unknown")
      .toLowerCase()
      .replace(/[^a-z0-9-]+/g, "-");
  const agentKey = (agent) => agent.work_id || agent.id;
  const readable = (value) => String(value || "unknown").replace(/[-_]+/g, " ");
  const titleCase = (value) =>
    readable(value).replace(/\b\w/g, (letter) => letter.toUpperCase());
  const localTime = (value) => {
    if (!value) return "Unavailable";
    const date = new Date(value);
    return Number.isNaN(date.getTime())
      ? "Unavailable"
      : date.toLocaleString([], {
          month: "short",
          day: "numeric",
          hour: "numeric",
          minute: "2-digit",
        });
  };
  const relativeTime = (value) => {
    if (!value) return "Not recorded";
    const ms = Date.now() - new Date(value).getTime();
    if (Number.isNaN(ms)) return "Unavailable";
    const min = Math.round(Math.abs(ms) / 60000);
    if (min < 1) return "Just now";
    if (min < 60) return `${min}m ${ms > 0 ? "ago" : "ahead"}`;
    const hours = Math.round(min / 60);
    if (hours < 48) return `${hours}h ${ms > 0 ? "ago" : "ahead"}`;
    return `${Math.round(hours / 24)}d ago`;
  };
  const number = (value) =>
    Number.isFinite(value)
      ? new Intl.NumberFormat().format(value)
      : "Unavailable";
  const current = () => state.snapshot || emptySnapshot();
  const emptySnapshot = () => ({
    schema_version: 1,
    generated_at: null,
    project: { name: "This project" },
    runtime: { available: true, emergency_stopped: false, message: null },
    summary: {},
    goals: [],
    jobs: [],
    agents: [],
    events: [],
    warnings: [],
  });
  const isFocusedIn = (container) =>
    container &&
    container.contains(document.activeElement) &&
    document.activeElement !== document.body;
  const status = (value, extra = "") => {
    const label = titleCase(value);
    const node = el(
      "span",
      `status status-${safeClass(value)} ${extra}`,
      label,
    );
    node.setAttribute("aria-label", `State: ${label}`);
    return node;
  };
  const textLine = (tag, className, text) => el(tag, className, text || "—");

  function savePreference(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch (_) {
      /* Storage can be disabled. */
    }
  }
  function loadPreference(key, fallback) {
    try {
      const value = localStorage.getItem(key);
      return value === null ? fallback : JSON.parse(value);
    } catch (_) {
      return fallback;
    }
  }

  function setConnection(kind, message) {
    const node = $("connection-status");
    node.className = `connection ${kind}`;
    node.replaceChildren(el("span", "signal"), el("span", "", message));
  }
  function setNotice(message, kind = "") {
    const node = $("notice-bar");
    node.hidden = !message;
    node.className = `notice-bar ${kind}`;
    node.textContent = message || "";
  }
  function hasNoTrackedWork(snapshot) {
    return !(
      snapshot.goals?.length ||
      snapshot.jobs?.length ||
      snapshot.agents?.length
    );
  }

  function metric(label, value, note, kind) {
    const card = el("article", `metric ${kind || ""}`);
    card.append(
      el("div", "metric-label", label),
      el("div", "metric-value", value),
      el("div", "metric-note", note),
    );
    return card;
  }
  function progressBar(percent) {
    const value = Number.isFinite(percent)
      ? Math.max(0, Math.min(100, percent))
      : null;
    const node = el("div", "progress");
    node.setAttribute("role", "progressbar");
    node.setAttribute("aria-label", "Goal completion");
    node.setAttribute("aria-valuemin", "0");
    node.setAttribute("aria-valuemax", "100");
    if (value === null) {
      node.setAttribute("aria-valuetext", "Completion unavailable");
    } else {
      node.setAttribute("aria-valuenow", String(value));
      node.append(el("b", "", ""));
      node.firstChild.style.width = `${value}%`;
    }
    return node;
  }
  function goalButton(goal, compact) {
    const button = el("button", "record-button");
    button.type = "button";
    button.dataset.action = "select-goal";
    button.dataset.goalId = goal.id;
    button.setAttribute(
      "aria-label",
      `Show details for goal ${goal.title || goal.id}`,
    );
    button.append(
      textLine("div", "record-title", goal.title || goal.id),
      textLine(
        "div",
        "record-subtitle",
        compact
          ? `${goal.jobs_complete || 0} of ${goal.jobs_total || 0} jobs complete`
          : `Updated ${relativeTime(goal.updated_at)}`,
      ),
    );
    return button;
  }
  function renderOverviewGoals(snapshot) {
    const target = $("overview-goals");
    if (isFocusedIn(target)) return;
    const goals = snapshot.goals.slice(0, 4);
    if (!goals.length) {
      target.replaceChildren(
        textLine(
          "p",
          "record-subtitle",
          "No goals have been recorded for this workspace.",
        ),
      );
      return;
    }
    const rows = goals.map((goal) => {
      const row = el("div", "goal-row");
      row.append(
        goalButton(goal, true),
        progressBar(goal.progress_percent),
        el(
          "div",
          "percent",
          Number.isFinite(goal.progress_percent)
            ? `${Math.round(goal.progress_percent)}%`
            : "—",
        ),
      );
      return row;
    });
    target.replaceChildren(...rows);
  }
  function agentIsActive(agent) {
    return ["started", "leased"].includes(agent.state) && !agent.lease_stale && !agent.heartbeat?.stale;
  }
  function agentAvatar(agent) {
    const avatar = el(
      "span",
      `agent-avatar agent-robot ${agentIsActive(agent) ? "robot-working" : ""}`,
    );
    avatar.setAttribute("aria-hidden", "true");
    // Draw the face explicitly so generic avatar styles cannot erase its eyes.
    const svgNode = (tag, attributes) => {
      const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
      Object.entries(attributes).forEach(([key, value]) =>
        node.setAttribute(key, value),
      );
      return node;
    };
    const portrait = svgNode("svg", {
      viewBox: "0 0 64 72",
      fill: "none",
      focusable: "false",
    });
    const parts = [
      ["path", { d: "M32 8v7", stroke: "#8be7ce", "stroke-width": "3" }],
      ["circle", { cx: "32", cy: "6", r: "4", fill: "#8be7ce" }],
      [
        "rect",
        { x: "4", y: "25", width: "7", height: "13", rx: "3", fill: "#9ab8c6" },
      ],
      [
        "rect",
        {
          x: "53",
          y: "25",
          width: "7",
          height: "13",
          rx: "3",
          fill: "#9ab8c6",
        },
      ],
      [
        "path",
        {
          d: "M15 53l-5 7m39-7 5 7",
          stroke: "#a9c7ce",
          "stroke-width": "6",
          "stroke-linecap": "round",
        },
      ],
      [
        "path",
        {
          d: "M24 62v5m16-5v5",
          stroke: "#a9c7ce",
          "stroke-width": "7",
          "stroke-linecap": "round",
        },
      ],
      [
        "rect",
        {
          x: "18",
          y: "46",
          width: "28",
          height: "18",
          rx: "6",
          fill: "#ff9375",
        },
      ],
      [
        "rect",
        {
          x: "9",
          y: "14",
          width: "46",
          height: "33",
          rx: "12",
          fill: "#e1f0f0",
        },
      ],
      [
        "rect",
        {
          x: "15",
          y: "20",
          width: "34",
          height: "22",
          rx: "8",
          fill: "#10283a",
        },
      ],
      [
        "rect",
        {
          x: "22",
          y: "25",
          width: "5",
          height: "7",
          rx: "2.5",
          fill: "#8be7ce",
        },
      ],
      [
        "rect",
        {
          x: "37",
          y: "25",
          width: "5",
          height: "7",
          rx: "2.5",
          fill: "#8be7ce",
        },
      ],
      [
        "path",
        {
          d: "M28 35q4 4 8 0",
          stroke: "#8be7ce",
          "stroke-width": "2",
          "stroke-linecap": "round",
        },
      ],
      [
        "path",
        {
          d: "M27 55h10",
          stroke: "#773f36",
          "stroke-width": "3",
          "stroke-linecap": "round",
        },
      ],
    ];
    parts.forEach(([tag, attributes]) =>
      portrait.append(svgNode(tag, attributes)),
    );
    avatar.append(portrait);
    return avatar;
  }
  function renderOverviewAgents(snapshot) {
    const target = $("overview-agents");
    if (isFocusedIn(target)) return;
    const agents = snapshot.agents
      .filter(
        (agent) =>
          !state.selectedGoalId || agent.goal_id === state.selectedGoalId,
      )
      .slice(0, 6);
    if (!agents.length) {
      target.replaceChildren(
        textLine(
          "span",
          "station-label",
          "Agent station awaiting recorded agents",
        ),
      );
      return;
    }
    const nodes = [textLine("span", "station-label", "Live agent station")];
    agents.forEach((agent) => {
      const card = el(
        "button",
        `station-agent ${agentIsActive(agent) ? "agent-active" : ""}`,
      );
      card.type = "button";
      card.dataset.action = "select-agent";
      card.dataset.agentId = agent.id;
      card.dataset.agentWorkId = agentKey(agent);
      card.setAttribute(
        "aria-label",
        `Show details for ${agent.role || agent.id}`,
      );
      card.append(
        agentAvatar(agent),
        textLine("span", "station-agent-name", agent.role || agent.id),
        status(agent.lease_stale ? "lease-stale" : agent.state),
      );
      nodes.push(card);
    });
    target.replaceChildren(...nodes);
  }
  function renderActivity(snapshot) {
    const target = $("activity-feed");
    if (isFocusedIn(target)) return;
    const goals = new Map(snapshot.goals.map((goal) => [goal.id, goal]));
    const jobs = new Map(snapshot.jobs.map((job) => [job.id, job]));
    const events = snapshot.events
      .filter(
        (event) =>
          !state.selectedGoalId || event.goal_id === state.selectedGoalId,
      )
      .slice(0, 6);
    if (!events.length) {
      target.replaceChildren(
        list("li", "", [
          textLine("span", "", "Events recorded by Tasktra will appear here."),
          textLine("time", "", "No activity yet"),
        ]),
      );
      return;
    }
    target.replaceChildren(
      ...events.map((event) => {
        const related =
          goals.get(event.goal_id)?.title ||
          jobs.get(event.work_unit_id)?.title ||
          event.goal_id ||
          event.work_unit_id ||
          "project workspace";
        const item = el("li");
        item.append(
          textLine(
            "span",
            "",
            `${titleCase(event.event_type || "recorded event")} \u00b7 ${related}`,
          ),
          textLine("time", "", localTime(event.created_at)),
        );
        return item;
      }),
    );
  }
  function renderGoals(snapshot) {
    const target = $("goals-grid");
    if (isFocusedIn(target)) return;
    if (!snapshot.goals.length) {
      target.replaceChildren(
        textLine("p", "record-subtitle", "No goals have been recorded."),
      );
      return;
    }
    const cards = snapshot.goals.map((goal) => {
      const card = el(
        "article",
        `goal-card ${state.selectedGoalId === goal.id ? "active" : ""}`,
      );
      const top = el("div", "goal-card-top");
      top.append(goalButton(goal, false), status(goal.status));
      const description = textLine(
        "p",
        "goal-description",
        goal.description || "No description recorded.",
      );
      const stats = el("div", "goal-stats");
      stats.append(
        textLine(
          "span",
          "",
          `${goal.jobs_complete || 0}/${goal.jobs_total || 0} jobs`,
        ),
        textLine(
          "span",
          "",
          `${goal.acceptance_recorded || 0}/${goal.acceptance_total || 0} checks`,
        ),
      );
      card.append(top, description, progressBar(goal.progress_percent), stats);
      return card;
    });
    target.replaceChildren(...cards);
  }
  function selectedJobs(snapshot) {
    const query = $("job-search").value.trim().toLowerCase();
    const selectedStatus = $("job-status-filter").value;
    return snapshot.jobs.filter((job) => {
      const text =
        `${job.title || ""} ${job.id || ""} ${job.owner_id || ""}`.toLowerCase();
      return (
        (!state.selectedGoalId || job.goal_id === state.selectedGoalId) &&
        (!query || text.includes(query)) &&
        (selectedStatus === "all" || job.status === selectedStatus)
      );
    });
  }
  function updateJobFilterOptions(snapshot) {
    const select = $("job-status-filter");
    const prior = select.value || "all";
    if (document.activeElement === select) return;
    const statuses = [
      ...new Set(snapshot.jobs.map((job) => job.status).filter(Boolean)),
    ].sort();
    select.replaceChildren(el("option", "", "All states"));
    select.firstChild.value = "all";
    statuses.forEach((value) => {
      const option = el("option", "", titleCase(value));
      option.value = value;
      select.append(option);
    });
    select.value = statuses.includes(prior) || prior === "all" ? prior : "all";
  }
  function detailItem(label, value) {
    const node = el("div", "detail-item");
    node.append(el("b", "", label), el("span", "", value));
    return node;
  }
  function refreshDetail(snapshot) {
    if (!state.selected) return;
    if (state.selected.type === "agent" && state.activity.workId === state.selected.id) return;
    const source =
      state.selected.type === "goal"
        ? snapshot.goals
        : state.selected.type === "job"
          ? snapshot.jobs
          : snapshot.agents;
    const record = source.find((item) =>
      state.selected.type === "agent"
        ? agentKey(item) === state.selected.id
        : item.id === state.selected.id,
    );
    if (record) showDetail(state.selected.type, record, false);
    else {
      state.selected = null;
      $("detail-panel").hidden = true;
    }
  }

  function baseSwitchView(view) {
    state.activeView = view;
    syncInsightsVisibility();
    ["overview", "goals", "jobs", "agents", "map", "timeline", "efficiency"].forEach((name) => {
      const tab = $(`tab-${name}`);
      const panel = $(`view-${name}`);
      tab.setAttribute("aria-selected", String(name === view));
      panel.hidden = name !== view;
    });
    document.querySelector(".dashboard-grid").hidden = view !== "overview";
    if (view !== "overview")
      window.setTimeout(
        () =>
          $(`view-${view}`).scrollIntoView({
            behavior: "smooth",
            block: "start",
          }),
        0,
      );
  }
  function applySnapshot(snapshot, options = {}) {
    const normalized = normalizeSnapshot(snapshot);
    const projectChanged = workspacePrepareSnapshot?.(normalized) || false;
    state.snapshot = normalized;
    state.lastError = null;
    state.retryMs = POLL_MS;
    if (!options.keepDemo) {
      state.demo = false;
      state.liveSnapshot = normalized;
    }
    setConnection("good", state.demo ? "Demo mode" : state.live ? "Connected" : "Updates paused");
    render(normalized);
    if (!projectChanged) workspaceObserveSnapshot?.(normalized);
    renderWorkspace?.(normalized);
    renderComparison?.(normalized);
    if (projectChanged) {
      workspaceNotice?.("Project identity changed. Cleared prior scope, filters, selections, pins, and saved-view namespace before reloading the new project.");
      window.setTimeout(() => { if (!state.demo && validWorkspaceKey(normalized.project?.key) === state.workspace.projectKey) fetchSnapshot({ force: true }); }, 0);
    }
  }
  function scheduleNext() {
    window.clearTimeout(state.timer);
    if (state.live && !state.demo && !state.workspace?.pending)
      state.timer = window.setTimeout(fetchSnapshot, state.retryMs);
  }
  function setDemoControls() {
    $("demo-button").textContent = state.demo ? "Exit demo" : "Explore demo";
    $("demo-header-button").textContent = state.demo ? "Exit demo" : "Demo";
  }
  function exitDemo() {
    supersedeWorkspaceRestore?.();
    state.modeGeneration += 1;
    state.demo = false;
    state.selectedGoalId = null;
    state.selected = null;
    closeAgentActivity({ clear: true });
    $("detail-panel").hidden = true;
    state.snapshot = state.liveSnapshot || emptySnapshot();
    render(state.snapshot);
    renderWorkspace?.(state.snapshot);
    renderComparison?.(state.snapshot);
    setDemoControls();
    fetchSnapshot({ force: true });
  }
  function refreshNow() {
    if (state.demo) exitDemo();
    state.retryMs = POLL_MS;
    fetchSnapshot({ force: true });
  }
  function demoSnapshot() {
    const now = new Date();
    const ago = (minutes) =>
      new Date(now.getTime() - minutes * 60000).toISOString();
    return normalizeSnapshot({
      generated_at: now.toISOString(),
      project: { name: "Northstar product launch" },
      runtime: { available: true, emergency_stopped: false },
      summary: {
        goals: 3,
        active_goals: 2,
        jobs: 7,
        completed_jobs: 2,
        running_jobs: 1,
        blocked_jobs: 1,
        agents: 4,
        running_agents: 1,
      },
      goals: [
        {
          id: "g-discovery",
          title: "Validate the activation path",
          description: "Find the smallest high-confidence path to first value.",
          status: "active",
          created_at: ago(4200),
          updated_at: ago(8),
          jobs_total: 4,
          jobs_complete: 1,
          progress_percent: 25,
          acceptance_total: 3,
          acceptance_recorded: 2,
          budget: {
            total_tokens: 80000,
            consumed_tokens: 42600,
            reserved_tokens: 12000,
          },
          checkpoints: [
            { id: "brief", status: "recorded" },
            { id: "review", status: "recorded" },
          ],
        },
        {
          id: "g-portal",
          title: "Make project state legible",
          description:
            "Give the team a calm, truthful view of work moving through Tasktra.",
          status: "active",
          created_at: ago(2060),
          updated_at: ago(16),
          jobs_total: 2,
          jobs_complete: 1,
          progress_percent: 50,
          acceptance_total: 2,
          acceptance_recorded: 1,
          budget: {
            total_tokens: null,
            consumed_tokens: 19000,
            reserved_tokens: 0,
          },
          checkpoints: [{ id: "design", status: "recorded" }],
        },
        {
          id: "g-release",
          title: "Ship the release note",
          description: "Share the verified behavior and operational notes.",
          status: "planned",
          created_at: ago(240),
          updated_at: ago(90),
          jobs_total: 1,
          jobs_complete: 0,
          progress_percent: 0,
          acceptance_total: 1,
          acceptance_recorded: 0,
          budget: {
            total_tokens: 20000,
            consumed_tokens: 0,
            reserved_tokens: 0,
          },
          checkpoints: [],
        },
      ],
      jobs: [
        {
          id: "job-research",
          goal_id: "g-discovery",
          title: "Map activation evidence",
          status: "complete",
          owner_id: "atlas",
          attempt_count: 1,
          heartbeat_at: ago(90),
          lease_expires_at: ago(80),
          lease_stale: false,
          updated_at: ago(90),
          last_outcome_class: "succeeded",
        },
        {
          id: "job-synthesis",
          goal_id: "g-discovery",
          title: "Synthesize customer signals",
          status: "leased",
          owner_id: "lumen",
          attempt_count: 1,
          heartbeat_at: ago(8),
          lease_expires_at: new Date(now.getTime() + 120000).toISOString(),
          lease_stale: false,
          updated_at: ago(8),
          last_outcome_class: null,
        },
        {
          id: "job-brief",
          goal_id: "g-discovery",
          title: "Write decision brief",
          status: "eligible",
          owner_id: null,
          attempt_count: 0,
          heartbeat_at: null,
          lease_expires_at: null,
          lease_stale: false,
          updated_at: ago(45),
          last_outcome_class: null,
        },
        {
          id: "job-design",
          goal_id: "g-portal",
          title: "Compose mission-control portal",
          status: "complete",
          owner_id: "nova",
          attempt_count: 1,
          heartbeat_at: ago(16),
          lease_expires_at: ago(15),
          lease_stale: false,
          updated_at: ago(16),
          last_outcome_class: "succeeded",
        },
        {
          id: "job-verify",
          goal_id: "g-portal",
          title: "Verify keyboard and empty state",
          status: "blocked",
          owner_id: "nova",
          attempt_count: 2,
          heartbeat_at: ago(32),
          lease_expires_at: ago(20),
          lease_stale: true,
          updated_at: ago(32),
          last_outcome_class: "blocked",
        },
        {
          id: "job-announce",
          goal_id: "g-release",
          title: "Draft release announcement",
          status: "planned",
          owner_id: null,
          attempt_count: 0,
          heartbeat_at: null,
          lease_expires_at: null,
          lease_stale: false,
          updated_at: ago(90),
          last_outcome_class: null,
        },
        {
          id: "job-checks",
          goal_id: "g-discovery",
          title: "Record acceptance checks",
          status: "retry-wait",
          owner_id: "atlas",
          attempt_count: 2,
          heartbeat_at: ago(55),
          lease_expires_at: ago(40),
          lease_stale: false,
          updated_at: ago(55),
          last_outcome_class: "failed",
        },
      ],
      agents: [
        {
          id: "atlas",
          work_id: "job-research",
          goal_id: "g-discovery",
          role: "research analyst",
          state: "succeeded",
          model: "gpt-6.1-sol",
          effort: "high",
          provenance: "recorded",
          total_tokens: 12300,
          heartbeat_at: ago(90),
          lease_expires_at: ago(80),
          lease_stale: false,
        },
        {
          id: "lumen",
          work_id: "job-synthesis",
          goal_id: "g-discovery",
          role: "product analyst",
          state: "started",
          model: "gpt-6.1-sol",
          effort: "high",
          provenance: "recorded",
          total_tokens: 8900,
          heartbeat_at: ago(8),
          lease_expires_at: new Date(now.getTime() + 120000).toISOString(),
          lease_stale: false,
        },
        {
          id: "nova",
          work_id: "job-verify",
          goal_id: "g-portal",
          role: "frontend specialist",
          state: "failed",
          model: "gpt-5.6-terra",
          effort: "high",
          provenance: "recorded",
          total_tokens: null,
          heartbeat_at: ago(32),
          lease_expires_at: ago(20),
          lease_stale: true,
        },
        {
          id: "quill",
          work_id: "job-announce",
          goal_id: "g-release",
          role: "release writer",
          state: "planned",
          model: "gpt-5.6-terra",
          effort: "medium",
          provenance: "recorded",
          total_tokens: null,
          heartbeat_at: null,
          lease_expires_at: null,
          lease_stale: false,
        },
      ],
      events: [
        {
          id: "e1",
          event_type: "job leased",
          goal_id: "g-discovery",
          work_unit_id: "job-synthesis",
          created_at: ago(8),
        },
        {
          id: "e2",
          event_type: "goal updated",
          goal_id: "g-portal",
          work_unit_id: null,
          created_at: ago(16),
        },
        {
          id: "e3",
          event_type: "job blocked",
          goal_id: "g-portal",
          work_unit_id: "job-verify",
          created_at: ago(32),
        },
        {
          id: "e4",
          event_type: "acceptance recorded",
          goal_id: "g-discovery",
          work_unit_id: "job-research",
          created_at: ago(90),
        },
      ],
    });
  }
  function startDemo() {
    supersedeWorkspaceRestore?.();
    state.modeGeneration += 1;
    state.demo = true;
    state.selectedGoalId = null;
    state.selected = null;
    closeAgentActivity({ clear: true });
    $("detail-panel").hidden = true;
    window.clearTimeout(state.timer);
    if (state.controller) state.controller.abort();
    applySnapshot(demoSnapshot(), { keepDemo: true });
    setDemoControls();
    setNotice(
      "Representative sample data is visible only in this browser. Exit demo to return to your local project.",
      "warning",
    );
  }
  function handleClick(event) {
    const action = event.target.closest("[data-action]")?.dataset;
    if (action?.action === "select-goal") {
      supersedeWorkspaceRestore?.();
      selectGoal(action.goalId);
      return;
    }
    if (action?.action === "select-job") {
      supersedeWorkspaceRestore?.();
      const record = current().jobs.find((job) => job.id === action.jobId);
      if (record) showDetail("job", record);
      return;
    }
    if (action?.action === "select-agent") {
      supersedeWorkspaceRestore?.();
      const record = current().agents.find(
        (agent) => agentKey(agent) === action.agentWorkId,
      );
      if (record) openAgentActivity(record);
      return;
    }
    if (action?.action === "pin-comparison") { pinComparison?.(action.workId); return; }
    if (action?.action === "open-comparison") { supersedeWorkspaceRestore?.(); const record = current().agents.filter((agent) => agent.work_id === action.workId); if (record.length === 1) openAgentActivity(record[0]); return; }
    const view =
      event.target.closest("[data-view]")?.dataset.view ||
      event.target.closest("[data-switch-view]")?.dataset.switchView;
    if (view) { supersedeWorkspaceRestore?.(); switchView(view); }
  }
  function bind() {
    document.addEventListener("click", handleClick);
    document
      .querySelector(".view-tabs")
      .addEventListener("keydown", (event) => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key))
          return;
        const tabs = [...document.querySelectorAll(".view-tabs [role=tab]")];
        const here = tabs.indexOf(document.activeElement);
        let next = here;
        if (event.key === "ArrowRight") next = (here + 1) % tabs.length;
        if (event.key === "ArrowLeft")
          next = (here - 1 + tabs.length) % tabs.length;
        if (event.key === "Home") next = 0;
        if (event.key === "End") next = tabs.length - 1;
        event.preventDefault();
        supersedeWorkspaceRestore?.();
        tabs[next].focus();
        switchView(tabs[next].dataset.view);
      });
    $("job-search").addEventListener("input", () => { supersedeWorkspaceRestore?.(); renderJobs(current()); });
    $("job-status-filter").addEventListener("change", () => {
      supersedeWorkspaceRestore?.(); renderJobs(current());
    });
    $("clear-job-filters").addEventListener("click", () => {
      supersedeWorkspaceRestore?.();
      $("job-search").value = "";
      $("job-status-filter").value = "all";
      render(current());
    });
    $("clear-goal-scope").addEventListener("click", () => changeGoalScope(""));
    $("close-detail").addEventListener("click", () => {
      supersedeWorkspaceRestore?.();
      $("detail-panel").hidden = true;
      state.selected = null;
    });
    $("demo-button").addEventListener("click", () => {
      if (state.demo) refreshNow();
      else startDemo();
    });
    $("demo-header-button").addEventListener("click", () => {
      if (state.demo) refreshNow();
      else startDemo();
    });
    $("refresh-button").addEventListener("click", () => { supersedeWorkspaceRestore?.(); refreshNow(); });
    $("live-toggle").addEventListener("click", () => {
      supersedeWorkspaceRestore?.();
      state.live = !state.live;
      $("live-toggle").textContent = `Live: ${state.live ? "on" : "paused"}`;
      $("live-toggle").setAttribute("aria-pressed", String(state.live));
      if (state.live) {
        state.retryMs = POLL_MS;
        fetchSnapshot();
      } else {
        window.clearTimeout(state.timer);
        if (state.controller) state.controller.abort();
        setNotice(
          "Live updates are paused. Use Refresh to request the newest local snapshot.",
          "warning",
        );
      }
      savePreference("tasktra-live", state.live);
    });
    $("motion-toggle").addEventListener("click", () => {
      state.motion = !state.motion;
      document.body.classList.toggle("motion-off", !state.motion);
      $("motion-toggle").setAttribute("aria-pressed", String(state.motion));
      savePreference("tasktra-motion", state.motion);
    });
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) {
        window.clearTimeout(state.timer);
      } else if (state.live && !state.demo && !state.workspace?.pending) {
        state.retryMs = POLL_MS;
        fetchSnapshot();
      }
    });
  }

  function isTerminal(record) {
    return ["complete", "completed", "succeeded", "failed", "cancelled", "canceled", "exhausted"].includes(String(record.state || record.status || record.outcome || "").toLowerCase());
  }
  function leaseHeartbeatLabel(record) {
    if (isTerminal(record)) return "N/A — finished";
    const beat = record.heartbeat || {};
    if (beat.kind === "none") return "No linked lease";
    const observed = beat.observed_at || record.heartbeat_at;
    if (!observed) return "No lease heartbeat recorded";
    return beat.stale || record.lease_stale ? `Lease stale \u00b7 observed ${relativeTime(observed)}` : `Lease observed ${relativeTime(observed)}`;
  }
  function observedActivityLabel(record) {
    const observed = record.last_observed_at || record.finished_at || record.started_at;
    return observed ? `Observed ${relativeTime(observed)}` : "No observed activity timestamp";
  }
  function scopedTitle(snapshot) {
    if (!state.selectedGoalId) return "All recorded goals";
    const goal = [...(snapshot.goals || []), ...(snapshot.goal_options || [])].find((item) => item.id === state.selectedGoalId);
    return goal ? goal.title || goal.id : `Goal ${state.selectedGoalId}`;
  }
  function setSelectOptions(id, items, label, preserve = true) {
    const select = $(id);
    if (!select || document.activeElement === select) return;
    const prior = preserve ? select.value : "";
    const first = select.options[0]?.cloneNode(true) || el("option", "", "All");
    select.replaceChildren(first);
    [...new Set(items.filter(Boolean))].sort((a, b) => String(a).localeCompare(String(b))).forEach((value) => {
      const option = el("option", "", label ? label(value) : readable(value));
      option.value = value;
      select.append(option);
    });
    if (prior && ![...select.options].some((item) => item.value === prior)) {
      const missing = el("option", "", `${label ? label(prior) : prior} (no matching records)`);
      missing.value = prior;
      select.append(missing);
    }
    select.value = prior;
  }
  function syncGoalScope(snapshot) {
    const select = $("goal-scope-select");
    if (document.activeElement !== select) {
      const prior = state.selectedGoalId || "";
      select.replaceChildren(el("option", "", "All recorded goals"));
      select.options[0].value = "";
      (snapshot.goal_options?.length ? snapshot.goal_options : snapshot.goals || []).forEach((goal) => {
        const option = el("option", "", `${goal.title || goal.id} \u00b7 ${titleCase(goal.status || "recorded")}`);
        option.value = goal.id;
        select.append(option);
      });
      if (prior && ![...select.options].some((option) => option.value === prior)) { const missing = el("option", "", `Goal ${prior}`); missing.value = prior; select.append(missing); }
      select.value = prior;
    }
    $("scope-goal-title").textContent = state.selectedGoalId ? `Scoped to ${scopedTitle(snapshot)}` : "All recorded goals";
    $("clear-goal-scope").hidden = !state.selectedGoalId;
  }
  function selectedAgents(snapshot) {
    const f = state.agentFilters;
    const query = f.query.trim().toLowerCase();
    const agents = (snapshot.agents || []).filter((agent) => {
      const text = `${agent.role || ""} ${agent.id || ""} ${agent.work_id || ""} ${agent.job_title || ""}`.toLowerCase();
      const model = agent.observed_model || agent.model || "";
      return (!query || text.includes(query)) && (!f.role || agent.role === f.role) && (!f.model || model === f.model) && (!f.state || agent.state === f.state);
    });
    return agents.sort((left, right) => f.sort === "tokens"
      ? (Number(right.total_tokens) || -1) - (Number(left.total_tokens) || -1)
      : String(right.last_observed_at || right.finished_at || right.started_at || "").localeCompare(String(left.last_observed_at || left.finished_at || left.started_at || "")));
  }
  function renderMetrics(snapshot) {
    const summary = snapshot.summary || {};
    const prefix = state.selectedGoalId ? "Scoped" : "Recorded";
    $("metrics").replaceChildren(
      metric(`${prefix} active goals`, number(summary.active_goals ?? 0), `${number(summary.goals ?? snapshot.goals.length)} in this view`, "accent"),
      metric(`${prefix} jobs in motion`, number(summary.running_jobs ?? 0), `${number(summary.jobs ?? snapshot.jobs.length)} recorded`, ""),
      metric(`${prefix} blocked attention`, number(summary.blocked_jobs ?? 0), "Jobs needing a decision", summary.blocked_jobs ? "alert" : ""),
      metric(`${prefix} active agents`, number(summary.running_agents ?? 0), `${number(summary.agents ?? snapshot.agents.length)} recorded`, ""),
    );
  }
  function renderJobs(snapshot) {
    const target = $("jobs-body");
    if (isFocusedIn(target)) return;
    const jobs = selectedJobs(snapshot);
    if (!jobs.length) {
      const cell = el("td", "record-subtitle", state.selectedGoalId ? "No jobs match this goal and the selected filters." : "No jobs match these filters.");
      cell.colSpan = 6; const row = el("tr"); row.append(cell); target.replaceChildren(row); return;
    }
    target.replaceChildren(...jobs.map((job) => {
      const row = el("tr"), name = el("td"), stateCell = el("td"), action = el("td");
      name.append(textLine("div", "record-title", job.title || job.id), textLine("div", "record-subtitle", job.id));
      stateCell.append(status(job.status));
      const button = el("button", "text-button details-trigger", "Details"); button.type = "button"; button.dataset.action = "select-job"; button.dataset.jobId = job.id; action.append(button);
      row.append(name, stateCell, el("td", "", job.owner_id || "Unassigned"), el("td", job.lease_stale ? "stale" : "", leaseHeartbeatLabel(job)), el("td", "", number(job.attempt_count ?? 0)), action);
      return row;
    }));
  }
  function renderAgents(snapshot) {
    const agents = selectedAgents(snapshot), all = snapshot.agents || [];
    setSelectOptions("agent-role-filter", all.map((agent) => agent.role), titleCase);
    setSelectOptions("agent-model-filter", all.map((agent) => agent.observed_model || agent.model), (value) => value);
    setSelectOptions("agent-state-filter", all.map((agent) => agent.state), titleCase);
    $("agents-active-count").textContent = `${number(agents.length)} matching agent${agents.length === 1 ? "" : "s"} \u00b7 ${number(agents.filter(agentIsActive).length)} currently recorded as active`;
    const target = $("agents-grid"); if (isFocusedIn(target)) return;
    if (!agents.length) { target.replaceChildren(textLine("p", "record-subtitle", state.selectedGoalId ? "No agents match this goal and the selected filters." : "No agents match these filters.")); return; }
    target.replaceChildren(...agents.map((agent) => {
      const card = el("article", `agent-card ${agentIsActive(agent) ? "agent-active" : ""}`), top = el("div", "agent-card-top"), detail = el("button", "record-button"), foot = el("div", "agent-foot");
      top.append(agentAvatar(agent), status(agent.state)); detail.type = "button"; detail.dataset.action = "select-agent"; detail.dataset.agentWorkId = agentKey(agent);
      detail.append(textLine("div", "agent-role", agent.role || agent.id), textLine("div", "agent-meta", [agent.observed_model || agent.model, agent.observed_effort || agent.effort].filter(Boolean).join(" \u00b7 ") || "Model unavailable"));
      foot.append(textLine("span", "agent-meta", observedActivityLabel(agent)), textLine("span", "agent-meta", Number.isFinite(agent.total_tokens) ? `${number(agent.total_tokens)} tokens` : "Usage unavailable"));
      if (typeof agent.work_id === "string" && agent.work_id) { const compare = el("button", "text-button agent-compare", "Compare"); compare.type = "button"; compare.dataset.action = "pin-comparison"; compare.dataset.workId = agent.work_id; const open = el("button", "text-button agent-compare", "Open"); open.type = "button"; open.dataset.action = "open-comparison"; open.dataset.workId = agent.work_id; foot.append(compare, open); }
      card.append(top, detail, foot); return card;
    }));
  }
  function usageReport(snapshot) {
    if (snapshot.analytics) return snapshot.analytics;
    if (!state.demo) return { summary: {}, by_model: [], time_series: [], coverage: { time_series_complete: false, reason: "Usage analytics unavailable; refresh to retry." } };
    const records = snapshot.agents || [], measured = records.filter((agent) => Number.isFinite(agent.total_tokens));
    const total = measured.reduce((sum, agent) => sum + agent.total_tokens, 0);
    return { summary: { records: records.length, measured_records: measured.length, unknown_records: records.length - measured.length, total_tokens: total || null, average_measured_tokens: measured.length ? total / measured.length : null, median_measured_tokens: null }, by_model: [], time_series: [], coverage: { undated_records: 0, time_series_complete: false, reason: "Demonstration data has no hourly usage events." } };
  }
  function pct(value) { return Number.isFinite(value) ? `${Math.round(value)}%` : "—"; }
  function renderTimeline(series, coverage) {
    const target = $("usage-timeline"), values = $("usage-timeline-values");
    target.replaceChildren();
    values.replaceChildren();
    const largest = Math.max(0, ...series.map((item) => item.total_tokens || 0));
    if (!series.length) target.append(textLine("p", "efficiency-note", "No hourly measured token events for this scope."));
    series.forEach((bucket) => {
      const point = el("div", "timeline-point"), bar = el("span", "timeline-bar");
      const label = el("span", "timeline-label", localTime(bucket.bucket_start));
      const amount = el("span", "timeline-value", new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(bucket.total_tokens || 0));
      bar.style.height = `${largest ? Math.max(1, Math.round(100 * (bucket.total_tokens || 0) / largest)) : 0}%`;
      point.title = `${localTime(bucket.bucket_start)}: ${number(bucket.total_tokens)} tokens`;
      point.append(amount, bar, label);
      target.append(point);
      const row = el("tr");
      row.append(el("td", "", localTime(bucket.bucket_start)), ...["total_tokens", "input_tokens", "cached_input_tokens", "output_tokens"].map((key) => el("td", "", number(bucket[key]))));
      values.append(row);
    });
    $("usage-timeline-note").textContent = coverage?.time_series_complete
      ? "Hourly buckets reflect measured token events in the selected range. Times are local."
      : `Historical timing is incomplete${coverage?.undated_records ? `: ${number(coverage.undated_records)} record(s) have missing timing` : ""}${coverage?.reason ? `. ${coverage.reason}` : "."}`;
  }
  function renderEfficiency(snapshot) {
    const report = usageReport(snapshot), totals = report.summary || {}, coverage = report.coverage || {}, measured = totals.measured_records || 0;
    $("efficiency-scope-description").textContent = `${state.selectedGoalId ? scopedTitle(snapshot) : "All recorded executions across this project"}${state.analytics.since ? " \u00b7 Only measured token events within the selected time range" : ""}.`;
    setSelectOptions("usage-model-filter", report.options?.models || (report.by_model || []).map((item) => item.model), (value) => value);
    setSelectOptions("usage-role-filter", report.options?.roles || (snapshot.agents || []).map((agent) => agent.role), titleCase);
    $("usage-model-filter").value = state.analytics.model;
    $("usage-role-filter").value = state.analytics.role;
    $("usage-since-filter").value = state.analytics.since;
    $("efficiency-coverage").textContent = measured ? `${number(measured)} measured run${measured === 1 ? "" : "s"} of ${number(totals.records ?? measured)} recorded \u00b7 ${number(totals.unknown_records || 0)} usage record(s) unknown.` : state.selectedGoalId ? "No recorded usage for this goal. Counts are scoped; no global usage is shown." : "No measured usage yet. Planned work does not count as a completed execution.";
    const metrics = $("efficiency-metrics"); metrics.replaceChildren();
    [["Recorded tokens", totals.total_tokens], ["Average measured run", Number.isFinite(totals.average_measured_tokens) ? Math.round(totals.average_measured_tokens) : null], ["Median measured run", totals.median_measured_tokens], ["Cache utilization", Number.isFinite(totals.cache_utilization_percent) ? pct(totals.cache_utilization_percent) : null], ["Uncached input", totals.uncached_input_tokens], ["Success rate", Number.isFinite(totals.success_rate_percent) ? pct(totals.success_rate_percent) : null]].forEach(([label, value]) => { const card = el("article", "panel efficiency-metric"); card.append(el("p", "eyebrow", label), el("strong", "efficiency-value", typeof value === "string" ? value : number(value))); metrics.append(card); });
    const input = totals.input_tokens, output = totals.output_tokens, mix = $("efficiency-mix"); mix.replaceChildren(); if (Number.isFinite(input) && Number.isFinite(output) && input + output) { const a = el("span", "efficiency-input"), b = el("span", "efficiency-output"); a.style.width = `${100 * input / (input + output)}%`; b.style.width = `${100 * output / (input + output)}%`; mix.append(a, b); }
    const label = `${number(input)} input \u00b7 ${number(output)} output \u00b7 ${number(totals.cached_input_tokens)} cached input`; mix.setAttribute("aria-label", label); $("efficiency-mix-label").textContent = label;
    const body = $("efficiency-roles"); body.replaceChildren(); (report.by_model || []).forEach((group) => { const row = el("tr"), share = Number.isFinite(group.total_tokens) && Number.isFinite(totals.total_tokens) && totals.total_tokens ? pct(100 * group.total_tokens / totals.total_tokens) : "—"; row.append(el("td", "", group.model || "Unknown model"), el("td", "", `${number(group.measured_records)} / ${number(group.records)}`), el("td", "", `${number(group.total_tokens)} / ${share}`), el("td", "", `${number(group.average_measured_tokens)} / ${number(group.median_measured_tokens)}`), el("td", "", pct(group.cache_utilization_percent)), el("td", "", number(group.uncached_input_tokens)), el("td", "", `${number(group.successful_records)} / ${number(group.failed_records)} / ${number(group.cancelled_records)} of ${number((group.successful_records || 0) + (group.failed_records || 0) + (group.cancelled_records || 0))} finished`)); body.append(row); });
    if (!body.children.length) { const cell = el("td", "efficiency-note", "No measured model comparison available for this scope."); cell.colSpan = 7; const row = el("tr"); row.append(cell); body.append(row); }
    renderTimeline(report.time_series || [], coverage);
  }
  function renderRelationshipMap(snapshot) {
    const api = window.TasktraRelationshipMap;
    if (!api) return;
    if (!state.relationshipMap) {
      state.relationshipMap = api.create($("relationship-map-host"), {
        onOpen({ type, record }) {
          supersedeWorkspaceRestore?.();
          if (type === "agent") { openAgentActivity(record); return; }
          if (type === "goal") { switchView("goals"); showDetail("goal", record); return; }
          if (type === "job") { switchView("jobs"); showDetail("job", record); }
        },
      });
    }
    const scopeKey = state.selectedGoalId || "all";
    const reset = state.relationshipMapScopeKey !== scopeKey || state.relationshipMapMode !== state.demo;
    state.relationshipMap.update(snapshot, { scopeKey, projectKey: snapshot.project?.key || "", demo: state.demo, active: state.activeView === "map", reset });
    state.relationshipMapScopeKey = scopeKey;
    state.relationshipMapMode = state.demo;
  }
  function timelineScope(snapshot) { return state.demo ? "representative browser-only sample" : state.selectedGoalId ? `goal scope: ${scopedTitle(snapshot)}` : "recorded project scope"; }
  function renderPortalTimeline(snapshot) { if (state.portalTimeline) state.portalTimeline.update(snapshot, { demo: state.demo, scope: timelineScope(snapshot) }); }
  function handleTimelineTarget(target) {
    supersedeWorkspaceRestore?.();
    const snapshot = current();
    if (target.type === "job") { const record = snapshot.jobs.find((item) => item.id === target.id); if (record) { switchView("jobs"); showDetail("job", record); return; } }
    if (target.type === "agent") { const record = snapshot.agents.find((item) => agentKey(item) === target.id); if (record) { openAgentActivity(record); return; } }
    setNotice("That exact timeline record is not loaded in the current scope.", "warning");
  }
  function initPortalTimeline() {
    const api = window.TasktraPortalTimeline;
    if (!api || typeof api.create !== "function") return;
    state.portalTimeline = api.create(document, { onTarget: handleTimelineTarget, getSnapshot: current, getContext: () => ({ demo: state.demo, scope: timelineScope(current()) }) });
    state.portalTimeline.bind(); renderPortalTimeline(current());
  }
  function outcomesScope(snapshot) { return state.demo ? "representative browser-only sample" : state.selectedGoalId ? `goal scope: ${scopedTitle(snapshot)}` : "recorded project scope"; }
  function renderPortalOutcomes(snapshot) { if (state.portalOutcomes) state.portalOutcomes.update(snapshot, { demo: state.demo, scope: outcomesScope(snapshot) }); }
  function showOutcomeJob(jobId) { supersedeWorkspaceRestore?.(); const record = current().jobs.find((job) => job.id === jobId); if (record) { switchView("jobs"); showDetail("job", record); } else setNotice("That recorded outcome job is not loaded in the current scope.", "warning"); }
  function initPortalOutcomes() { const api = window.TasktraPortalOutcomes; if (!api || typeof api.create !== "function") return; state.portalOutcomes = api.create(document, { onJob: showOutcomeJob }); renderPortalOutcomes(current()); }
  function showDetail(type, record, shouldFocus = true) {
    state.selected = { type, id: type === "agent" ? agentKey(record) : record.id };
    const title = type === "goal" ? record.title || record.id : type === "job" ? record.title || record.id : record.role || record.id;
    $("detail-kind").textContent = `${titleCase(type)} record`; $("detail-title").textContent = title;
    const grid = el("div", "detail-grid");
    if (type === "agent") {
      const total = record.total_tokens, input = record.input_tokens, cached = record.cached_input_tokens, output = record.output_tokens;
      grid.append(detailItem("State / outcome", [titleCase(record.state), record.outcome ? titleCase(record.outcome) : null].filter(Boolean).join(" \u00b7 ")), detailItem("Job", record.job_title || "No linked job recorded"), detailItem("Goal lineage", record.goal_title || [...(current().goal_options || []), ...current().goals].find((goal) => goal.id === record.goal_id)?.title || record.goal_id || "Unassigned"), detailItem("Observed model / effort", [record.observed_model || record.model || "Unavailable", record.observed_effort || record.effort || "Unavailable"].join(" \u00b7 ")), detailItem("Configured model / effort", [record.configured_model || "Unavailable", record.configured_effort || "Unavailable"].join(" \u00b7 ")), detailItem("Token total", number(total)), detailItem("Input / uncached input", `${number(input)} / ${number(record.uncached_input_tokens)}`), detailItem("Cached input / utilization", `${number(cached)} / ${Number.isFinite(cached) && Number.isFinite(input) && input ? pct(100 * cached / input) : "—"}`), detailItem("Output", number(output)), detailItem("First usage observed", localTime(record.started_at)), detailItem("Last observed", localTime(record.last_observed_at)), detailItem("Lease heartbeat", leaseHeartbeatLabel(record)), detailItem("Usage evidence", record.unknown_reason === "rollout-image-line-over-limit" ? "Unavailable: an image entry exceeded the safe import limit" : record.unknown_reason || (Number.isFinite(total) ? "Measured usage available" : "No measured usage recorded")));
      const technical = el("details", "technical-details"), summary = el("summary", "", "Technical identifiers and provenance"), data = el("div", "detail-grid"); data.append(detailItem("Agent ID", record.id || "Unavailable"), detailItem("Work ID", record.work_id || "Unavailable"), detailItem("Thread ID", record.thread_id || "Unavailable"), detailItem("Turn ID", record.turn_id || "Unavailable"), detailItem("Parent work ID", record.parent_work_id || "Unavailable"), detailItem("Provenance", record.provenance || "Unavailable")); technical.append(summary, data); $("detail-content").replaceChildren(grid, el("p", "detail-note", "Observed activity comes from execution records. A lease heartbeat records Tasktra coordination; it does not establish whether a host process is still running."), technical);
    } else {
    if (type === "goal") {
      const budget = record.budget || {};
      const checkpoints = record.checkpoints?.length
        ? record.checkpoints
            .map(
              (checkpoint) =>
                `${checkpoint.id || "Checkpoint"}: ${titleCase(checkpoint.status)}`,
            )
            .join(" \u00b7 ")
        : "None recorded";
      grid.append(
        detailItem("State", titleCase(record.status)),
        detailItem(
          "Progress",
          Number.isFinite(record.progress_percent)
            ? `${Math.round(record.progress_percent)}%`
            : "Unavailable",
        ),
        detailItem(
          "Acceptance",
          `${record.acceptance_recorded || 0}/${record.acceptance_total || 0} recorded`,
        ),
        detailItem("Checkpoints", checkpoints),
        detailItem(
          "Budget consumed",
          budget.consumed_tokens === undefined
            ? "Unavailable"
            : number(budget.consumed_tokens),
        ),
        detailItem(
          "Budget total",
          budget.total_tokens === null || budget.total_tokens === undefined
            ? "Unavailable"
            : number(budget.total_tokens),
        ),
        detailItem(
          "Reserved",
          budget.reserved_tokens === undefined
            ? "Unavailable"
            : number(budget.reserved_tokens),
        ),
        detailItem("Updated", localTime(record.updated_at)),
        detailItem("Created", localTime(record.created_at)),
      );
    } else if (type === "job") {
      grid.append(
        detailItem("State", titleCase(record.status)),
        detailItem("Owner", record.owner_id || "Unassigned"),
        detailItem("Attempts", number(record.attempt_count ?? 0)),
        detailItem("Lease heartbeat", leaseHeartbeatLabel(record)),
        detailItem(
          "Lease",
          record.lease_stale
            ? "Stale"
            : record.lease_expires_at
              ? `Expires ${localTime(record.lease_expires_at)}`
              : "Unavailable",
        ),
        detailItem(
          "Last outcome",
          record.last_outcome_class
            ? titleCase(record.last_outcome_class)
            : "Unavailable",
        ),
        detailItem("Updated", localTime(record.updated_at)),
        detailItem("Goal", record.goal_id || "Unlinked"),
      );
    }
      if (type === "job") {
        const api = window.TasktraPortalOutcomes, report = api?.normalizeOutcomes?.(current().insights?.outcomes), outcome = report?.jobs?.find((job) => job.job_id === record.id);
        const evidenceScroll = Object.fromEntries([...$("detail-content").querySelectorAll("[data-evidence-scroll]")].map((node) => [node.dataset.evidenceScroll, node.scrollTop]));
        const evidence = api?.jobEvidence ? api.jobEvidence(document, outcome) : el("section", "panel job-evidence-panel", "Outcome evidence is unavailable."); evidence.id = "job-evidence-panel";
        $("detail-content").replaceChildren(grid, evidence);
        evidence.querySelectorAll("[data-evidence-scroll]").forEach((node) => { node.scrollTop = evidenceScroll[node.dataset.evidenceScroll] || 0; });
      } else $("detail-content").replaceChildren(grid);
    }
    $("detail-panel").hidden = false; if (shouldFocus) $("detail-title").focus({ preventScroll: true });
  }

  function resetLocalFilters() {
    state.agentFilters = { query: "", role: "", model: "", state: "", sort: "recent" };
    state.analytics = { model: "", role: "", since: "" };
    ["agent-search", "agent-role-filter", "agent-model-filter", "agent-state-filter", "usage-model-filter", "usage-role-filter", "usage-since-filter", "job-search"].forEach((id) => { $(id).value = ""; });
    $("agent-sort").value = "recent";
    $("job-status-filter").value = "all";
  }
  function renderDemoScope() {
    const snapshot = demoSnapshot();
    snapshot.goal_options = snapshot.goals.map(({ id, title, status }) => ({ id, title, status }));
    if (state.selectedGoalId) {
      ["goals", "jobs", "agents", "events"].forEach((key) => {
        snapshot[key] = snapshot[key].filter((item) => (key === "goals" ? item.id : item.goal_id) === state.selectedGoalId);
      });
      snapshot.summary = {
        goals: snapshot.goals.length, active_goals: snapshot.goals.filter((goal) => goal.status === "active").length,
        jobs: snapshot.jobs.length, running_jobs: snapshot.jobs.filter((job) => job.status === "leased").length,
        blocked_jobs: snapshot.jobs.filter((job) => job.status === "blocked").length,
        agents: snapshot.agents.length, running_agents: snapshot.agents.filter(agentIsActive).length,
      };
    }
    state.snapshot = snapshot;
    render(snapshot);
    renderWorkspace?.(snapshot);
    renderComparison?.(snapshot);
  }
  function changeGoalScope(id) {
    supersedeWorkspaceRestore?.();
    state.selectedGoalId = id || null;
    state.selected = null;
    closeAgentActivity({ clear: true });
    $("detail-panel").hidden = true;
    resetLocalFilters();
    if (state.demo) { renderDemoScope(); return; }
    fetchSnapshot({ force: true, replace: true });
  }
  function selectGoal(id) {
    const goal = current().goals.find((item) => item.id === id);
    changeGoalScope(state.selectedGoalId === id ? "" : id);
    if (goal) showDetail("goal", goal);
  }
  function fetchQuery(override = {}) {
    const params = new URLSearchParams(), goalId = Object.hasOwn(override, "goalId") ? override.goalId : state.selectedGoalId, analytics = override.analytics || state.analytics;
    if (goalId) params.set("goal_id", goalId);
    if (analytics.model) params.set("model", analytics.model);
    if (analytics.role) params.set("role", analytics.role);
    const periods = { P1D: 1, P7D: 7, P30D: 30 };
    if (analytics.since && periods[analytics.since]) {
      params.set("since", new Date(Date.now() - periods[analytics.since] * 86400000).toISOString());
    }
    return params.toString();
  }
  async function fetchSnapshot(options = {}) {
    const force = Boolean(options.force);
    if ((!state.live && !force) || state.demo) return;
    window.clearTimeout(state.timer);
    if (state.controller) state.controller.abort();
    const controller = new AbortController();
    const sequence = ++state.requestSequence;
    const mode = state.modeGeneration;
    state.controller = controller;
    state.requestInFlight = true;
    let timedOut = false;
    const timeout = window.setTimeout(() => { timedOut = true; controller.abort(); }, 8000);
    try {
      const query = fetchQuery(options.query || {});
      const response = await fetch(`/api/snapshot${query ? `?${query}` : ""}`, {
        headers: { Accept: "application/json" }, signal: controller.signal, cache: "no-store",
      });
      if (!response.ok) throw new Error(`Snapshot request returned ${response.status}`);
      const snapshot = await response.json();
      if (sequence !== state.requestSequence || mode !== state.modeGeneration || state.demo || (options.workspaceRestoreGeneration && options.workspaceRestoreGeneration !== state.workspace.restoreGeneration)) return { ok: false, superseded: true };
      applySnapshot(snapshot);
      return { ok: true, snapshot: state.snapshot, sequence, mode };
    } catch (error) {
      if (sequence !== state.requestSequence || mode !== state.modeGeneration || state.demo || (options.workspaceRestoreGeneration && options.workspaceRestoreGeneration !== state.workspace.restoreGeneration)) return { ok: false, superseded: true };
      if (error.name === "AbortError" && !timedOut) return { ok: false, superseded: true };
      state.lastError = error;
      setConnection("offline", state.snapshot ? "Stale data" : "Disconnected");
      setNotice("Could not refresh this scope. " + (state.live ? "Retrying shortly." : "Use Refresh to retry."), "warning");
      state.retryMs = Math.min(Math.round(state.retryMs * 1.8), MAX_BACKOFF_MS);
      return { ok: false, error };
    } finally {
      window.clearTimeout(timeout);
      if (sequence === state.requestSequence) {
        if (state.controller === controller) state.controller = null;
        state.requestInFlight = false;
        scheduleNext();
      }
    }
  }
  function normalizeSnapshot(value) {
    const input = value && typeof value === "object" ? value : {}; const normalized = {
      schema_version: input.schema_version || 1, generated_at: input.generated_at || null, project: input.project && typeof input.project === "object" ? input.project : { name: "This project" }, runtime: input.runtime && typeof input.runtime === "object" ? input.runtime : { available: false, emergency_stopped: false, message: "Runtime response was incomplete." }, summary: input.summary && typeof input.summary === "object" ? input.summary : {}, global_summary: input.global_summary && typeof input.global_summary === "object" ? input.global_summary : {}, goal_options: Array.isArray(input.goal_options) ? input.goal_options : [], analytics: input.analytics && typeof input.analytics === "object" ? input.analytics : null, insights: input.insights && typeof input.insights === "object" ? input.insights : null, efficiency: input.efficiency && typeof input.efficiency === "object" ? input.efficiency : null, goals: Array.isArray(input.goals) ? input.goals : [], jobs: Array.isArray(input.jobs) ? input.jobs : [], agents: Array.isArray(input.agents) ? input.agents : [], events: Array.isArray(input.events) ? input.events : [], warnings: Array.isArray(input.warnings) ? input.warnings : [] };
    return normalized;
  }
  function render(snapshot) {
    const project = snapshot.project?.name || "This project", runtime = snapshot.runtime || {}, summary = snapshot.summary || {};
    $("page-title").textContent = project; $("project-summary").textContent = `${number(summary.goals ?? snapshot.goals.length)} recorded goal${(summary.goals ?? snapshot.goals.length) === 1 ? "" : "s"} \u00b7 ${number(summary.jobs ?? snapshot.jobs.length)} recorded job${(summary.jobs ?? snapshot.jobs.length) === 1 ? "" : "s"}.`;
    $("last-updated").textContent = snapshot.generated_at ? `Last updated ${localTime(snapshot.generated_at)}` : "No snapshot timestamp available"; $("mode-label").textContent = state.demo ? "Representative demo \u00b7 client only" : state.selectedGoalId ? `Goal scope \u00b7 ${scopedTitle(snapshot)}` : "Live workspace"; $("footer-state").textContent = state.demo ? "Demo data remains in this browser only" : runtime.available ? "Reading local project state" : "Runtime state unavailable";
    $("goal-tab-count").textContent = number(summary.goals ?? snapshot.goals.length); $("job-tab-count").textContent = number(summary.jobs ?? snapshot.jobs.length); $("agent-tab-count").textContent = number(summary.agents ?? snapshot.agents.length); syncGoalScope(snapshot); renderMetrics(snapshot); renderPortalInsights(snapshot); renderOverviewGoals(snapshot); renderOverviewAgents(snapshot); renderActivity(snapshot); renderGoals(snapshot); updateJobFilterOptions(snapshot); renderJobs(snapshot); renderAgents(snapshot); syncActivityAgent(snapshot); renderEfficiency(snapshot); renderRelationshipMap(snapshot); renderPortalTimeline(snapshot); renderPortalOutcomes(snapshot); refreshDetail(snapshot);
    const empty = hasNoTrackedWork(snapshot) && !state.demo && !state.selectedGoalId; $("empty-state").hidden = !empty; document.querySelectorAll(".metrics,.view-tabs").forEach((node) => { node.hidden = empty; }); document.querySelector(".dashboard-grid").hidden = empty || state.activeView !== "overview"; ["overview", "goals", "jobs", "agents", "map", "timeline", "efficiency"].forEach((name) => { $(`view-${name}`).hidden = empty || name !== state.activeView; });
    const notices = [...snapshot.warnings.map(String)]; if (!state.demo && runtime.emergency_stopped) notices.unshift(runtime.message || "Tasktra execution is emergency-stopped. Recorded state remains available."); else if (!state.demo && runtime.available === false) notices.unshift(runtime.message || "The Tasktra runtime is unavailable. The portal will retry."); else if (state.demo) notices.unshift("Representative sample data is visible only in this browser. Exit demo to return to your local project."); if (!state.live) notices.unshift("Live updates are paused. Use Refresh to request the newest local snapshot."); if (!state.lastError) setNotice(notices.join(" \u00b7 "), notices.length ? "warning" : "");
  }
  function bindInsights() {
    $("goal-scope-select").addEventListener("change", (event) => changeGoalScope(event.target.value));
    [["agent-search", "query", "input"], ["agent-role-filter", "role", "change"], ["agent-model-filter", "model", "change"], ["agent-state-filter", "state", "change"], ["agent-sort", "sort", "change"]].forEach(([id, key, event]) => $(id).addEventListener(event, (e) => { supersedeWorkspaceRestore?.(); state.agentFilters[key] = e.target.value; renderAgents(current()); }));
    $("clear-agent-filters").addEventListener("click", () => { supersedeWorkspaceRestore?.(); state.agentFilters = { query: "", role: "", model: "", state: "", sort: "recent" }; $("agent-search").value = ""; $("agent-role-filter").value = ""; $("agent-model-filter").value = ""; $("agent-state-filter").value = ""; $("agent-sort").value = "recent"; renderAgents(current()); });
    [["usage-model-filter", "model"], ["usage-role-filter", "role"], ["usage-since-filter", "since"]].forEach(([id, key]) => $(id).addEventListener("change", (event) => { supersedeWorkspaceRestore?.(); state.analytics[key] = event.target.value; state.selected = null; $("detail-panel").hidden = true; if (state.demo) render(current()); else fetchSnapshot({ force: true, replace: true }); }));
    $("clear-usage-filters").addEventListener("click", () => { supersedeWorkspaceRestore?.(); state.analytics = { model: "", role: "", since: "" }; $("usage-model-filter").value = ""; $("usage-role-filter").value = ""; $("usage-since-filter").value = ""; if (state.demo) render(current()); else fetchSnapshot({ force: true, replace: true }); });
  }
  function baseInit() {
    state.live = loadPreference("tasktra-live", true); state.motion = loadPreference("tasktra-motion", !window.matchMedia("(prefers-reduced-motion: reduce)").matches); state.selectedGoalId = null; document.body.classList.toggle("motion-off", !state.motion); $("motion-toggle").setAttribute("aria-pressed", String(state.motion)); $("live-toggle").setAttribute("aria-pressed", String(state.live)); $("live-toggle").textContent = `Live: ${state.live ? "on" : "paused"}`; bind(); bindInsights(); render(emptySnapshot()); setConnection("", state.live ? "Connecting" : "Updates paused"); if (state.live) fetchSnapshot(); else { setNotice("Live updates are paused. The current local snapshot was loaded once; use Refresh for a newer one.", "warning"); fetchSnapshot({ force: true }); }
  }


  function syncActivityAgent(snapshot) {
    if (!state.activity.workId) return;
    const updated = (snapshot.agents || []).find((agent) => agent.work_id === state.activity.workId);
    if (!updated) { closeAgentActivity({ clear: true }); return; }
    state.activity.agent = updated;
    if (!$("agent-activity-panel").hidden) renderAgentActivity();
  }
  function switchView(view) {
    if (view !== "agents" && state.activity.workId) closeAgentActivity({ clear: true });
    baseSwitchView(view);
    if (state.relationshipMap && typeof state.relationshipMap.setActive === "function") state.relationshipMap.setActive(view === "map");
  }
  function activityGoal(agent) { return agent.goal_title || [...(current().goal_options || []), ...(current().goals || [])].find((goal) => goal.id === agent.goal_id)?.title || agent.goal_id || "Unassigned"; }
  function activityDemo(agent) {
    const now = new Date(), ago = (minutes) => new Date(now.getTime() - minutes * 60000).toISOString();
    return { available: true, source: "verified-rollout", state: agent.state, phase: "working", last_activity_at: ago(1), usage: { observed_at: ago(1), input_tokens: 5600, cached_input_tokens: 2100, output_tokens: 1800, total_tokens: 7400 }, coverage: { partial: true, reason: "Representative sample only" }, events: [{ id: "sample-progress", timestamp: ago(5), kind: "progress", text: "Reviewed the recorded task context." }, { id: "sample-tool", timestamp: ago(3), kind: "tool", tool_name: "workspace search", text: "Checked the selected project files." }, { id: "sample-status", timestamp: ago(1), kind: "status", state: "working", text: "Reported progress is current in this sample." }] };
  }
  function clearAgentActivityTimer() { window.clearTimeout(state.activity.timer); state.activity.timer = null; }
  function closeAgentActivity(options = {}) {
    const closedWorkId = state.activity.workId;
    clearAgentActivityTimer(); state.activity.sequence += 1;
    if (state.activity.controller) state.activity.controller.abort();
    state.activity.controller = null; $("agent-activity-panel").hidden = true;
    if (options.clear) {
      Object.assign(state.activity, { workId: null, agent: null, data: null, feedSignature: null });
      if (state.selected?.type === "agent" && state.selected.id === closedWorkId) state.selected = null;
    }
  }
  function activityItem(label, value) { const item = el("div", "detail-item"); item.append(el("b", "", label), el("span", "", value)); return item; }
  function activityUsage(usage) { return usage && Number.isFinite(usage.total_tokens) ? `${number(usage.total_tokens)} total \u00b7 ${number(usage.input_tokens)} input \u00b7 ${number(usage.cached_input_tokens)} cached \u00b7 ${number(usage.output_tokens)} output` : "No reported usage"; }
  const ACTIVITY_KINDS = { progress: "Progress", tool: "Tools", status: "Status", final: "Final output" };
  function renderAgentActivityFeed(events) {
    const allEvents = events || [], kind = state.activity.kind;
    const visible = kind === "all" ? allEvents : allEvents.filter((event) => event.kind === kind);
    $("agent-activity-filters").querySelectorAll("input[name='activity-kind']").forEach((input) => {
      input.checked = input.value === kind;
      const count = input.value === "all" ? allEvents.length : allEvents.filter((event) => event.kind === input.value).length;
      const badge = $(`activity-count-${input.value}`), countText = number(count);
      if (badge.textContent !== countText) badge.textContent = countText;
    });
    const summary = $("agent-activity-filter-summary"), summaryText = `Showing ${number(visible.length)} of ${number(allEvents.length)} loaded events.`;
    if (summary.textContent !== summaryText) summary.textContent = summaryText;
    const feed = $("agent-activity-feed");
    const signature = JSON.stringify([kind, visible.map((event) => [event.id, event.timestamp, event.kind, event.text, event.tool_name, event.state])]);
    if (signature === state.activity.feedSignature) return;
    const filterChanged = kind !== state.activity.renderedKind, priorTop = feed.scrollTop;
    state.activity.feedSignature = signature; state.activity.renderedKind = kind;
    if (!visible.length) {
      const message = !allEvents.length ? "No reported activity yet." : `No ${ACTIVITY_KINDS[kind].toLowerCase()} events in the loaded activity. Choose All activity to see other categories.`;
      feed.replaceChildren(el("li", "agent-activity-empty", message)); return;
    }
    feed.replaceChildren(...visible.map((event) => {
      const item = el("li", `agent-activity-event activity-kind-${safeClass(event.kind)}`), parts = [];
      if (event.kind === "tool" && event.tool_name) parts.push(event.tool_name);
      if (event.state) parts.push(titleCase(event.state));
      if (event.text && event.text !== event.tool_name) parts.push(event.text);
      item.append(el("span", "agent-activity-kind", Object.hasOwn(ACTIVITY_KINDS, event.kind) ? ACTIVITY_KINDS[event.kind] : "Other"), el("div", "", parts.join(" \u00b7 ") || "Reported activity"), el("time", "", localTime(event.timestamp))); return item;
    }));
    feed.scrollTop = state.activity.follow ? feed.scrollHeight : filterChanged ? 0 : priorTop;
  }
  function renderToolSpans(data) {
    const spans = Array.isArray(data?.tool_spans) ? data.tool_spans : [], list = $("agent-tool-spans-list"), note = $("agent-tool-spans-note");
    const signature = JSON.stringify([spans, Boolean(data?.tool_spans_partial)]);
    if (signature === state.activity.toolSpanSignature) return;
    state.activity.toolSpanSignature = signature;
    note.textContent = !spans.length ? (data?.tool_spans_partial ? "No complete verified tool spans were recorded; available coverage is partial." : "No verified tool durations were recorded.") : `${spans.length} verified tool span${spans.length === 1 ? "" : "s"} recorded.${data?.tool_spans_partial ? " Coverage is partial." : ""}`;
    list.replaceChildren(...spans.map((span) => {
      const item = el("li", "agent-tool-span"), hasTimes = span?.started_at && span?.ended_at && Number.isFinite(span?.duration_ms);
      item.append(el("strong", "", span?.tool_name || "Recorded tool"), el("span", "", hasTimes ? `${Math.round(span.duration_ms / 1000)} seconds · ${localTime(span.started_at)} to ${localTime(span.ended_at)}` : "Timing incomplete; duration not recorded"), el("span", "", span?.state ? `State: ${titleCase(span.state)}` : "State not recorded"));
      return item;
    }));
  }
  function renderAgentActivity(focus = false) {
    const panel = $("agent-activity-panel"), agent = state.activity.agent;
    if (!agent) { panel.hidden = true; return; }
    panel.hidden = false; $("agent-activity-title").textContent = agent.role || agent.id || "Selected agent"; $("agent-activity-kicker").textContent = state.demo ? "Sample reported progress" : "Live activity";
    const data = state.activity.data, job = agent.job_id ? agent.job_title || agent.job_id : "No linked job recorded";
    $("agent-activity-context").textContent = `${current().project?.name || "This project"} \u00b7 Goal: ${activityGoal(agent)} \u00b7 Job: ${job}`;
    $("agent-activity-summary").replaceChildren(activityItem("State", titleCase(data?.state || agent.state || "unknown")), activityItem("Last reported phase", titleCase(data?.phase || "unknown")), activityItem("Last observed", localTime(data?.last_activity_at || agent.last_observed_at)), activityItem("Latest reported usage", activityUsage(data?.usage)), activityItem("Last imported total", Number.isFinite(agent.total_tokens) ? `${number(agent.total_tokens)} tokens` : "No imported total"));
    $("agent-activity-details-content").replaceChildren(
      activityItem("Observed model / effort", [agent.observed_model || agent.model || "Unavailable", agent.observed_effort || agent.effort || "Unavailable"].join(" \u00b7 ")),
      activityItem("Requested model / effort", [agent.requested_model || "Unavailable", agent.requested_effort || "Unavailable"].join(" \u00b7 ")),
      activityItem("Configured model / effort", [agent.configured_model || "Unavailable", agent.configured_effort || "Unavailable"].join(" \u00b7 ")),
      activityItem("Imported token breakdown", `${number(agent.input_tokens)} input / ${number(agent.cached_input_tokens)} cached / ${number(agent.uncached_input_tokens)} uncached / ${number(agent.output_tokens)} output`),
      activityItem("Lease heartbeat", leaseHeartbeatLabel(agent)), activityItem("Provenance", agent.provenance || "Unavailable"),
      activityItem("Agent ID", agent.id || "Unavailable"), activityItem("Work ID", agent.work_id || "Unavailable"),
      activityItem("Thread ID", agent.thread_id || "Unavailable"), activityItem("Turn ID", agent.turn_id || "Unavailable"),
      activityItem("Parent work ID", agent.parent_work_id || "Unavailable"));
    const goal = $("agent-activity-goal"), jobButton = $("agent-activity-job"); goal.hidden = !agent.goal_id; jobButton.hidden = !agent.job_id;
    $("agent-activity-follow").setAttribute("aria-pressed", String(state.activity.follow)); $("agent-activity-follow").textContent = `Follow output: ${state.activity.follow ? "on" : "off"}`;
    $("agent-activity-status").textContent = state.demo ? "Representative sample activity shown in this browser only." : !state.live ? "Live updates paused." : state.activity.error ? `${state.activity.error} Showing the last reported activity.` : !data ? "Loading reported progress..." : !data.available || data.source === "none" ? data.coverage?.reason ? `Reported activity unavailable: ${data.coverage.reason}.` : "Reported activity unavailable." : data.coverage?.partial ? `Reported progress may be partial${data.coverage.reason ? `: ${data.coverage.reason}` : "."}` : "Last checked activity is from the verified rollout.";
    renderToolSpans(data); renderAgentActivityFeed(data?.events || []);
    if (focus) { panel.scrollIntoView({ behavior: "smooth", block: "start" }); $("agent-activity-title").focus({ preventScroll: true }); }
  }
  function scheduleAgentActivity() { clearAgentActivityTimer(); if (state.activity.workId && state.live && !state.demo) state.activity.timer = window.setTimeout(fetchAgentActivity, POLL_MS); }
  async function fetchAgentActivity(options = {}) {
    const workId = state.activity.workId;
    if (!workId || state.demo || (!state.live && !options.force)) return;
    if (state.activity.controller) state.activity.controller.abort();
    const controller = new AbortController(), sequence = ++state.activity.sequence, goalId = state.selectedGoalId, mode = state.modeGeneration;
    state.activity.controller = controller;
    try {
      const response = await fetch(`/api/agent-activity?${new URLSearchParams({ work_id: workId }).toString()}`, { headers: { Accept: "application/json" }, signal: controller.signal, cache: "no-store" });
      if (!response.ok) throw new Error(`Agent activity request returned ${response.status}`);
      const data = await response.json();
      if (sequence !== state.activity.sequence || goalId !== state.selectedGoalId || mode !== state.modeGeneration || state.activity.workId !== workId) return;
      state.activity.error = ""; state.activity.data = data; renderAgentActivity();
    } catch (error) {
      if (sequence !== state.activity.sequence || error.name === "AbortError") return;
      if (state.activity.data) state.activity.error = "Could not refresh reported activity.";
      else state.activity.data = { available: false, source: "none", coverage: { partial: true, reason: "Could not refresh reported activity" }, events: [] };
      renderAgentActivity();
    } finally { if (state.activity.controller === controller) state.activity.controller = null; if (sequence === state.activity.sequence) scheduleAgentActivity(); }
  }
  function openAgentActivity(agent) {
    state.selected = typeof agent?.work_id === "string" && agent.work_id ? { type: "agent", id: agent.work_id } : null; $("detail-panel").hidden = true; clearAgentActivityTimer(); if (state.activity.controller) state.activity.controller.abort(); state.activity.sequence += 1;
    Object.assign(state.activity, { workId: agent.work_id || null, agent, data: state.demo ? activityDemo(agent) : null, feedSignature: null, error: "" });
    switchView("agents"); renderAgentActivity(true); if (!state.demo && state.activity.workId) fetchAgentActivity({ force: true });
  }
  function refreshNow() { supersedeWorkspaceRestore?.(); if (state.demo) { exitDemo(); return; } state.retryMs = POLL_MS; fetchSnapshot({ force: true }); fetchAgentActivity({ force: true }); }
  function bindAgentActivity() {
    $("agent-activity-filters").addEventListener("change", (event) => {
      const input = event.target;
      if (input.name !== "activity-kind" || (input.value !== "all" && !Object.hasOwn(ACTIVITY_KINDS, input.value))) return;
      supersedeWorkspaceRestore?.(); state.activity.kind = input.value; renderAgentActivity();
    });
    $("close-agent-activity").addEventListener("click", () => { supersedeWorkspaceRestore?.(); closeAgentActivity({ clear: true }); });
    $("agent-activity-follow").addEventListener("click", () => { supersedeWorkspaceRestore?.(); state.activity.follow = !state.activity.follow; if (state.activity.follow) $("agent-activity-feed").scrollTop = $("agent-activity-feed").scrollHeight; renderAgentActivity(); });
    $("agent-activity-goal").addEventListener("click", () => { supersedeWorkspaceRestore?.(); const goal = current().goals.find((item) => item.id === state.activity.agent?.goal_id); closeAgentActivity({ clear: true }); switchView("goals"); if (goal) showDetail("goal", goal); });
    $("agent-activity-job").addEventListener("click", () => { supersedeWorkspaceRestore?.(); const job = current().jobs.find((item) => item.id === state.activity.agent?.job_id); closeAgentActivity({ clear: true }); switchView("jobs"); if (job) showDetail("job", job); });
    $("live-toggle").addEventListener("click", () => { if (state.live) fetchAgentActivity({ force: true }); else { clearAgentActivityTimer(); state.activity.sequence += 1; if (state.activity.controller) state.activity.controller.abort(); state.activity.controller = null; renderAgentActivity(); } });
  }
  function insightScope(snapshot) {
    return state.demo ? "Representative browser-only sample" : state.selectedGoalId ? `Goal scope: ${scopedTitle(snapshot)}` : "Recorded project scope";
  }
  function syncInsightsVisibility(snapshot = current()) {
    const visible = state.activeView === "overview" || (hasNoTrackedWork(snapshot) && !state.demo && !state.selectedGoalId);
    $("attention-panel").hidden = !visible;
    $("diagnostics-panel").hidden = !visible;
  }
  function renderPortalInsights(snapshot) {
    syncInsightsVisibility(snapshot);
    if (state.portalInsights) state.portalInsights.update(snapshot, { demo: state.demo, scope: insightScope(snapshot) });
  }
  function handleInsightTarget(target) {
    supersedeWorkspaceRestore?.();
    const snapshot = current();
    if (target.type === "diagnostics") { const panel = $("diagnostics-panel"); panel.open = true; $("diagnostics-summary").focus({ preventScroll: true }); panel.scrollIntoView({ behavior: state.motion ? "smooth" : "auto", block: "start" }); return; }
    if (target.type === "goal") { const record = snapshot.goals.find((item) => item.id === target.id); if (record) { switchView("goals"); showDetail("goal", record); return; } }
    if (target.type === "job") { const record = snapshot.jobs.find((item) => item.id === target.id); if (record) { switchView("jobs"); showDetail("job", record); return; } }
    if (target.type === "agent") { const record = snapshot.agents.find((item) => agentKey(item) === target.id); if (record) { openAgentActivity(record); return; } }
    setNotice("That recorded attention target is not loaded in the current scope.", "warning");
  }
  function initPortalInsights() {
    const api = window.TasktraPortalInsights;
    if (!api || typeof api.create !== "function") return;
    state.portalInsights = api.create(document, { onTarget: handleInsightTarget, getInsights: () => current().insights, isDemo: () => state.demo, scope: () => insightScope(current()) });
    state.portalInsights.bind();
    renderPortalInsights(current());
  }
  function workspaceApi() {
    const api = window.TasktraPortalWorkspace;
    return api && ["normalizeSettings", "makeEnvelope", "parseFragment", "serializeFragment", "readViews", "upsertView", "removeView", "normalizeComparison", "comparisonRows"].every((key) => typeof api[key] === "function") ? api : null;
  }
  function workspaceMode() { return state.demo ? "demo" : "live"; }
  function validWorkspaceKey(value) { return typeof value === "string" && /^[A-Za-z0-9_-]{1,128}$/.test(value) ? value : null; }
  function workspaceContext(snapshot = current()) {
    const projectKey = state.demo ? state.workspace.liveProjectKey : validWorkspaceKey(snapshot?.project?.key);
    return projectKey ? { projectKey, mode: workspaceMode() } : null;
  }
  function workspaceStorage() {
    try { return window.localStorage; } catch (_) { return null; }
  }
  function workspaceNotice(message) {
    state.workspace.notice = message || "";
    const node = $("workspace-notice");
    if (node) node.textContent = state.workspace.notice;
  }
  function workspaceAllowed(snapshot) {
    const unique = (items) => [...new Set((items || []).filter((value) => typeof value === "string" && value))];
    const insightItems = snapshot?.insights?.attention?.items || [];
    const timelineRows = snapshot?.insights?.timeline?.rows || [];
    return {
      goal_ids: unique([...(snapshot?.goal_options || []), ...(snapshot?.goals || [])].map((item) => item?.id)),
      job_ids: unique((snapshot?.jobs || []).map((item) => item?.id)),
      agent_work_ids: unique((snapshot?.agents || []).map((item) => item?.work_id)),
      roles: unique([...(snapshot?.agents || []).map((item) => item?.role), ...(snapshot?.analytics?.options?.roles || [])]),
      models: unique([...(snapshot?.agents || []).map((item) => item?.observed_model || item?.model), ...(snapshot?.analytics?.options?.models || []), ...timelineRows.map((item) => item?.model)]),
      job_states: unique((snapshot?.jobs || []).map((item) => item?.status)),
      agent_states: unique((snapshot?.agents || []).map((item) => item?.state)),
      attention_categories: unique(insightItems.map((item) => item?.category)),
      timeline_states: unique(timelineRows.map((item) => item?.state)),
    };
  }
  function workspaceSettings() {
    const insightFilters = state.portalInsights?.getFilters?.() || { severity: "", category: "" };
    const timelineFilters = state.portalTimeline?.getFilters?.() || { kind: "", state: "", model: "" };
    return {
      view: state.activeView,
      goal_id: state.selectedGoalId || "",
      jobs: { query: $("job-search").value || "", status: $("job-status-filter").value === "all" ? "" : $("job-status-filter").value || "" },
      agents: { ...state.agentFilters },
      usage: { ...state.analytics },
      attention: insightFilters,
      timeline: timelineFilters,
      map: { preset: state.relationshipMap?.getPreset?.() || "all" },
      activity: { kind: state.activity.kind, follow: Boolean(state.activity.follow) },
      selected: state.activity.workId && typeof state.activity.workId === "string" ? { type: "agent", id: state.activity.workId } : state.selected ? { type: state.selected.type, id: state.selected.id } : null,
      comparison: { work_ids: [...state.workspace.comparisonWorkIds] },
    };
  }
  function newWorkspaceId() {
    if (window.crypto?.randomUUID) return window.crypto.randomUUID().replace(/-/g, "");
    return `${Date.now().toString(16)}${Math.random().toString(16).slice(2)}`.padEnd(16, "0").slice(0, 32);
  }
  function renderWorkspace(snapshot = current()) {
    const api = workspaceApi(), context = workspaceContext(snapshot), disabled = !api || !context || state.workspace.pending;
    ["workspace-save", "workspace-restore", "workspace-delete", "workspace-copy-link"].forEach((id) => { const node = $(id); if (node) node.disabled = disabled; });
    const select = $("workspace-list");
    if (!api || !context) {
      if (select && document.activeElement !== select) { select.replaceChildren(el("option", "", "Saved views unavailable")); select.value = ""; }
      if (!state.workspace.pending) workspaceNotice(state.demo && !state.workspace.liveProjectKey ? "Saved views need a verified live project identity before demo mode can use its separate workspace." : "Saved views will be available after the project identity is verified.");
      return;
    }
    const loaded = api.readViews(workspaceStorage(), context.projectKey, context.mode);
    state.workspace.views = loaded.value?.views || [];
    if (!loaded.ok) workspaceNotice("Saved views are unavailable in this browser. Existing storage was left unchanged.");
    else if (state.workspace.notice === "Saved views will be available after the project identity is verified.") workspaceNotice("");
    if (select && document.activeElement !== select) {
      const wanted = state.workspace.selectedViewId;
      select.replaceChildren(el("option", "", "Choose a saved view")); select.firstChild.value = "";
      state.workspace.views.forEach((view) => { const option = el("option", "", view.name); option.value = view.id; select.append(option); });
      select.value = state.workspace.views.some((view) => view.id === wanted) ? wanted : "";
    }
    const save = $("workspace-save");
    if (save) save.textContent = state.workspace.views.some((view) => view.id === state.workspace.selectedViewId) ? "Update selected view" : "Save new view";
  }
  function supersedeWorkspaceRestore(changedControl = null) {
    if (!state.workspace.pending) return;
    const previous = state.workspace.requested?.previous;
    const focused = changedControl && changedControl.isConnected && "value" in changedControl ? changedControl : document.activeElement;
    const focusedValue = focused && "value" in focused ? focused.value : undefined;
    state.workspace.restoreGeneration += 1;
    state.workspace.pending = false;
    state.workspace.requested = null;
    if (previous?.settings) {
      workspaceApplyControls(previous.settings);
      state.selected = previous.selected;
      if (focused && focused.isConnected && focusedValue !== undefined && "value" in focused) focused.value = focusedValue;
      render(current());
      if (focused && focused.isConnected && focusedValue !== undefined && "value" in focused) focused.value = focusedValue;
    }
    workspaceNotice("View restoration was superseded by a newer action.");
  }
  function workspaceApplyControls(settings) {
    state.selectedGoalId = settings.goal_id || null;
    state.analytics = { ...settings.usage };
    state.agentFilters = { ...settings.agents };
    $("job-search").value = settings.jobs.query;
    $("job-status-filter").value = settings.jobs.status || "all";
    $("agent-search").value = settings.agents.query;
    $("agent-role-filter").value = settings.agents.role;
    $("agent-model-filter").value = settings.agents.model;
    $("agent-state-filter").value = settings.agents.state;
    $("agent-sort").value = settings.agents.sort;
    $("usage-model-filter").value = settings.usage.model;
    $("usage-role-filter").value = settings.usage.role;
    $("usage-since-filter").value = settings.usage.since;
    state.portalInsights?.setFilters?.(settings.attention);
    state.portalTimeline?.setFilters?.(settings.timeline);
    state.relationshipMap?.setPreset?.(settings.map.preset);
    state.activity.kind = settings.activity.kind;
    state.activity.follow = settings.activity.follow;
    state.workspace.comparisonWorkIds = [...settings.comparison.work_ids];
  }
  function resolveWorkspaceIdentities(settings, snapshot) {
    let note = "";
    state.selected = null;
    $("detail-panel").hidden = true;
    closeAgentActivity({ clear: true });
    if (settings.selected) {
      const source = settings.selected.type === "goal" ? snapshot.goals : settings.selected.type === "job" ? snapshot.jobs : snapshot.agents;
      const matches = (source || []).filter((item) => settings.selected.type === "agent" ? item?.work_id === settings.selected.id : item?.id === settings.selected.id);
      if (matches.length === 1) {
        if (settings.selected.type === "agent") openAgentActivity(matches[0]);
        else showDetail(settings.selected.type, matches[0], false);
      } else note = "The saved selected record is not available in this scope.";
    }
    const pins = workspaceApi()?.normalizeComparison(state.workspace.comparisonWorkIds, snapshot.agents || []);
    if (pins && pins.value.pins.some((pin) => !pin.found)) note = `${note}${note ? " " : ""}One or more saved comparison records are unavailable in this scope.`;
    return note;
  }
  async function restoreWorkspaceSettings(settings, meta = {}) {
    const api = workspaceApi(), context = workspaceContext();
    if (state.workspace.pending && meta.source === "link") supersedeWorkspaceRestore();
    if (!api || !context || state.workspace.pending) { workspaceNotice(state.workspace.pending ? "A view is still restoring; wait for it to finish." : "Saved views are unavailable for this project."); return; }
    const generation = ++state.workspace.restoreGeneration;
    const immutable = JSON.parse(JSON.stringify(settings));
    const previous = { settings: JSON.parse(JSON.stringify(workspaceSettings())), selected: state.selected ? { ...state.selected } : null, activityWorkId: state.activity.workId || null };
    state.workspace.pending = true;
    state.workspace.requested = { generation, settings: immutable, context: { ...context }, source: meta.source || "saved", previous };
    workspaceNotice("Restoring this view…");
    if (state.controller) state.controller.abort();
    state.selectedGoalId = immutable.goal_id || null;
    state.analytics = { ...immutable.usage };
    if (state.demo) {
      renderDemoScope();
      const normalized = api.normalizeSettings(immutable, workspaceAllowed(current()));
      if (generation !== state.workspace.restoreGeneration || !state.workspace.pending) return;
      workspaceFinishRestore(normalized, current(), generation);
      return;
    }
    const result = await fetchSnapshot({ force: true, workspaceRestoreGeneration: generation });
    if (generation !== state.workspace.restoreGeneration || !state.workspace.pending) return;
    if (!result?.ok || !result.snapshot) {
      const previous = state.workspace.requested?.previous;
      state.workspace.pending = false; state.workspace.requested = null;
      if (previous?.settings) {
        workspaceApplyControls(previous.settings);
        state.selected = previous.selected;
        render(current());
      } else {
        state.selected = null; state.workspace.comparisonWorkIds = [];
      }
      workspaceNotice("Could not restore this view because its requested scope could not be loaded. The prior view remains active."); renderWorkspace(current()); renderComparison(current()); return;
    }
    const latestContext = workspaceContext(result.snapshot);
    if (!latestContext || latestContext.projectKey !== context.projectKey || latestContext.mode !== context.mode) {
      state.workspace.pending = false; state.workspace.requested = null; state.selected = null; state.workspace.comparisonWorkIds = [];
      workspaceNotice("This view belongs to a different project or mode and was not restored."); renderWorkspace(current()); renderComparison(current()); return;
    }
    workspaceFinishRestore(api.normalizeSettings(immutable, workspaceAllowed(result.snapshot)), result.snapshot, generation);
  }
  function workspaceFinishRestore(normalized, snapshot, generation) {
    if (generation !== state.workspace.restoreGeneration || !state.workspace.pending) return;
    workspaceApplyControls(normalized.value);
    const selectedAgent = normalized.ok && normalized.value.selected?.type === "agent";
    switchView(selectedAgent ? "agents" : normalized.value.view);
    const identityNotice = normalized.ok ? resolveWorkspaceIdentities(normalized.value, snapshot) : "Some saved filters are no longer available in this scope and were cleared.";
    const notice = selectedAgent && normalized.value.view !== "agents" ? `Opened Agents to show the saved selected agent.${identityNotice ? ` ${identityNotice}` : ""}` : identityNotice;
    state.workspace.pending = false; state.workspace.requested = null;
    render(snapshot); renderComparison(snapshot); renderWorkspace(snapshot);
    workspaceNotice(notice || "Saved view restored.");
  }
  function clearWorkspaceProjectState(key) {
    state.workspace.restoreGeneration += 1;
    state.workspace.pending = false;
    state.workspace.requested = null;
    state.workspace.projectKey = key;
    state.workspace.liveProjectKey = key;
    state.workspace.hashHandled = false;
    state.workspace.views = [];
    state.workspace.selectedViewId = "";
    state.workspace.comparisonWorkIds = [];
    state.workspace.comparisonSignature = null;
    state.selectedGoalId = null;
    state.selected = null;
    resetLocalFilters();
    closeAgentActivity({ clear: true });
    $("detail-panel").hidden = true;
    baseSwitchView("overview");
  }
  function workspacePrepareSnapshot(snapshot) {
    const key = validWorkspaceKey(snapshot?.project?.key);
    if (state.demo || !key) return false;
    const previous = state.workspace.projectKey;
    if (previous && previous !== key) {
      clearWorkspaceProjectState(key);
      return true;
    }
    state.workspace.projectKey = key;
    state.workspace.liveProjectKey = key;
    return false;
  }
  function workspaceObserveSnapshot(snapshot) {
    const key = validWorkspaceKey(snapshot?.project?.key);
    if (state.workspace.hashHandled || !workspaceApi() || !key || state.demo) return;
    state.workspace.hashHandled = true;
    if (!window.location.hash) return;
    const parsed = workspaceApi().parseFragment(window.location.hash, {});
    if (!parsed.ok || !parsed.value) { workspaceNotice("This view link is invalid or unsupported."); return; }
    const context = workspaceContext(snapshot);
    if (!context || parsed.value.project_key !== context.projectKey || parsed.value.mode !== context.mode) { workspaceNotice("This view link belongs to a different project or mode."); return; }
    restoreWorkspaceSettings(parsed.value.settings, { source: "link" });
  }
  function displayComparisonValue(value, percent = false) { return Number.isFinite(value) ? (percent ? `${Math.round(value * 100)}%` : number(value)) : "Unknown"; }
  function renderComparison(snapshot = current(), forcePickers = false) {
    const panel = $("comparison-panel"), api = workspaceApi();
    if (!panel || !api) return;
    const pins = api.normalizeComparison(state.workspace.comparisonWorkIds, snapshot.agents || []);
    const value = pins.value || { requested: [], pins: [], ready: false };
    const counts = new Map();
    (snapshot.agents || []).forEach((agent) => counts.set(agent.work_id, (counts.get(agent.work_id) || 0) + 1));
    const options = (snapshot.agents || []).filter((agent) => typeof agent.work_id === "string" && agent.work_id && counts.get(agent.work_id) === 1);
    const syncPicker = (id, currentId) => { const select = $(id); if (!select || (!forcePickers && document.activeElement === select)) return; select.replaceChildren(el("option", "", "Choose an agent")); select.firstChild.value = ""; options.forEach((agent) => { const option = el("option", "", `${agent.role || agent.work_id} · ${agent.work_id}`); option.value = agent.work_id; select.append(option); }); if (currentId && !options.some((agent) => agent.work_id === currentId)) { const missing = el("option", "", `${currentId} (unavailable)`); missing.value = currentId; select.append(missing); } select.value = currentId || ""; };
    syncPicker("comparison-first-picker", value.requested[0] || ""); syncPicker("comparison-second-picker", value.requested[1] || "");
    panel.hidden = false;
    const statusNode = $("comparison-status"), grid = $("comparison-grid");
    if (!value.requested.length) { statusNode.textContent = "Choose Compare on two recorded agents to inspect imported snapshot values."; grid.replaceChildren(); state.workspace.comparisonSignature = null; return; }
    const result = api.comparisonRows(value.pins[0], value.pins[1]).value;
    statusNode.textContent = value.requested.length === 1 && value.pins[0]?.found ? "Choose a second agent to compare with the first recorded agent." : result.stale ? "One or more pinned records are unavailable in the current scope. No replacement was selected." : "Imported snapshot values only. Difference is second agent minus first agent. Observation timestamps do not measure execution duration.";
    const signature = JSON.stringify([value, result]);
    if (signature === state.workspace.comparisonSignature) return;
    const focused = grid.contains(document.activeElement) ? document.activeElement : null;
    const focusKey = focused?.dataset.action ? [focused.dataset.action, focused.dataset.workId || "", focused.dataset.goalId || "", focused.dataset.jobId || "", focused.closest("th, td")?.cellIndex] : null;
    state.workspace.comparisonSignature = signature; grid.replaceChildren();
    const left = result.left?.agent, right = result.right?.agent;
    const table = el("table", "comparison-table"), head = el("thead", ""), header = el("tr", ""), body = el("tbody", "");
    const heading = (label, agent) => { const cell = el("th", "comparison-heading"); cell.scope = "col"; if (agent) { const button = el("button", "text-button", label); button.type = "button"; button.dataset.action = "open-comparison"; button.dataset.workId = agent.work_id; button.title = "Open recorded agent activity"; cell.append(button); } else cell.textContent = label; header.append(cell); };
    heading("Metric"); heading(left?.work_id || value.requested[0] || "First", left); heading(right?.work_id || value.requested[1] || "Second", right); heading("Second − first");
    head.append(header); table.append(head, body); grid.append(table);
    const add = (label, leftValue, rightValue, diff = "") => { const row = el("tr", ""), metric = el("th", "comparison-metric", label); metric.scope = "row"; row.append(metric); for (const value of [leftValue, rightValue]) { const cell = el("td", ""); if (value instanceof Node) cell.append(value); else cell.textContent = value || "Unknown"; row.append(cell); } row.append(el("td", diff ? "comparison-difference" : "comparison-unknown", diff || "Not comparable")); body.append(row); };
    add("Role", left?.role, right?.role); add("Model / effort", [left?.model, left?.effort].filter(Boolean).join(" · "), [right?.model, right?.effort].filter(Boolean).join(" · "));
    add("State", left?.state, right?.state);
    const contextCell = (agent) => { const cell = el("div", "comparison-context"); if (!agent?.goal_id && !agent?.job_id) { cell.textContent = "Unknown"; return cell; } if (agent.goal_id) { const goal = el("button", "text-button", `Goal ${agent.goal_id}`); goal.type = "button"; goal.dataset.action = "select-goal"; goal.dataset.goalId = agent.goal_id; cell.append(goal); } if (agent.job_id) { const job = el("button", "text-button", `Job ${agent.job_id}`); job.type = "button"; job.dataset.action = "select-job"; job.dataset.jobId = agent.job_id; cell.append(job); } return cell; };
    add("Goal / job", contextCell(left), contextCell(right));
    add("Reported start", localTime(left?.started_at), localTime(right?.started_at)); add("Last observed", localTime(left?.last_observed_at), localTime(right?.last_observed_at)); add("Outcome", left?.outcome, right?.outcome);
    const coverage = (agent) => Number.isSafeInteger(agent?.usage?.total_tokens) ? "Measured imported values" : "Not measured";
    add("Usage coverage", coverage(left), coverage(right)); add("Record source", left?.provenance, right?.provenance);
    result.metrics.forEach((row) => add(titleCase(row.label), displayComparisonValue(row.left, row.key === "cache_fraction"), displayComparisonValue(row.right, row.key === "cache_fraction"), row.comparable ? (row.key === "cache_fraction" ? `${(row.difference * 100).toFixed(1)} percentage points` : displayComparisonValue(row.difference)) : ""));
    if (focusKey) Array.from(grid.querySelectorAll("button[data-action]")).find((button) => [button.dataset.action, button.dataset.workId || "", button.dataset.goalId || "", button.dataset.jobId || "", button.closest("th, td")?.cellIndex].every((value, index) => value === focusKey[index]))?.focus({ preventScroll: true });
  }
  function pinComparison(workId) {
    if (typeof workId !== "string" || !workId) return;
    supersedeWorkspaceRestore();
    const existing = state.workspace.comparisonWorkIds.filter((id) => id !== workId);
    state.workspace.comparisonWorkIds = existing.length >= 2 ? [existing[1], workId] : [...existing, workId];
    state.workspace.comparisonSignature = null; renderComparison(current()); switchView("agents");
  }
  function bindWorkspace() {
    $("workspace-save").addEventListener("click", () => {
      if (state.workspace.pending) { workspaceNotice("Wait for view restoration to finish before saving."); return; }
      const api = workspaceApi(), context = workspaceContext(); if (!api || !context) { workspaceNotice("Saved views are unavailable for this project."); return; }
      const name = $("workspace-name").value.trim(), existing = state.workspace.views.find((view) => view.id === state.workspace.selectedViewId);
      if (!name && !existing) { workspaceNotice("Enter a name to save a new view."); return; }
      const settings = api.normalizeSettings(workspaceSettings(), workspaceAllowed(current()));
      if (!settings.ok) { workspaceNotice("This view contains filters that are not available in the current snapshot."); return; }
      const entry = { id: existing?.id || newWorkspaceId(), name: name || existing.name, settings: settings.value, updated_at: new Date().toISOString() };
      const saved = api.upsertView(workspaceStorage(), context.projectKey, context.mode, entry);
      state.workspace.views = saved.value?.views || state.workspace.views;
      if (saved.ok && saved.value?.saved) { state.workspace.selectedViewId = saved.value.saved.id; $("workspace-name").value = saved.value.saved.name; workspaceNotice(existing ? "Saved view updated." : "Saved view created."); } else workspaceNotice("Could not save this view. Existing browser storage was not changed.");
      renderWorkspace(current());
    });
    $("workspace-list").addEventListener("change", (event) => { state.workspace.selectedViewId = event.target.value || ""; const view = state.workspace.views.find((item) => item.id === state.workspace.selectedViewId); $("workspace-name").value = view?.name || ""; });
    $("workspace-restore").addEventListener("click", () => { const view = state.workspace.views.find((item) => item.id === state.workspace.selectedViewId); if (!view) { workspaceNotice("Choose a saved view to restore."); return; } restoreWorkspaceSettings(view.settings, { source: "saved" }); });
    $("workspace-delete").addEventListener("click", () => { const api = workspaceApi(), context = workspaceContext(), id = state.workspace.selectedViewId; if (!api || !context || !id) { workspaceNotice("Choose a saved view to delete."); return; } const result = api.removeView(workspaceStorage(), context.projectKey, context.mode, id); state.workspace.views = result.value?.views || state.workspace.views; if (result.ok && result.value?.removed) { state.workspace.selectedViewId = ""; $("workspace-name").value = ""; workspaceNotice("Saved view deleted."); } else workspaceNotice("Could not delete that saved view. Existing browser storage was not changed."); renderWorkspace(current()); });
    $("workspace-copy-link").addEventListener("click", async () => { const api = workspaceApi(), context = workspaceContext(); if (state.workspace.pending) { workspaceNotice("Wait for view restoration to finish before copying a link."); return; } if (!api || !context) { workspaceNotice("A verified project identity is needed before copying a view link."); return; } const settings = api.normalizeSettings(workspaceSettings(), workspaceAllowed(current())); const envelope = settings.ok && api.makeEnvelope(settings.value, context); const fragment = envelope?.ok && api.serializeFragment(envelope.value); if (!fragment?.ok) { workspaceNotice("This view could not be encoded as a shareable link."); return; } const link = `${window.location.href.split("#")[0]}${fragment.value}`; const fallback = $("workspace-link-fallback"), wrap = $("workspace-link-fallback-wrap"); fallback.value = link; try { if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable"); await navigator.clipboard.writeText(link); wrap.hidden = true; workspaceNotice("View link copied. It does not change the current URL."); } catch (_) { wrap.hidden = false; fallback.focus(); fallback.select(); workspaceNotice("Copy is unavailable here. Select the read-only link to copy it manually."); } });
    ["comparison-first-picker", "comparison-second-picker"].forEach((id, index) => {
      $(id).addEventListener("change", (event) => { supersedeWorkspaceRestore(); const ids = [...state.workspace.comparisonWorkIds]; ids[index] = event.target.value || ""; state.workspace.comparisonWorkIds = ids.filter(Boolean); if (!ids[0] && ids[1]) workspaceNotice("The remaining agent is now first. Choose a second agent to compare."); state.workspace.comparisonSignature = null; renderComparison(current(), true); });
      $(id).addEventListener("blur", () => renderComparison(current(), true));
    });
    $("comparison-clear").addEventListener("click", () => { supersedeWorkspaceRestore(); state.workspace.comparisonWorkIds = []; state.workspace.comparisonSignature = null; renderComparison(current()); });
    window.addEventListener("hashchange", () => { supersedeWorkspaceRestore(); const api = workspaceApi(), context = workspaceContext(); if (!api || !context) return; state.workspace.hashHandled = true; const parsed = api.parseFragment(window.location.hash, {}); if (!parsed.ok || !parsed.value) { workspaceNotice("This view link is invalid or unsupported."); return; } if (parsed.value.project_key !== context.projectKey || parsed.value.mode !== context.mode) { workspaceNotice("This view link belongs to a different project or mode."); return; } restoreWorkspaceSettings(parsed.value.settings, { source: "link" }); });
  }
  function initWorkspace() {
    bindWorkspace();
    document.addEventListener("change", (event) => {
      if (["attention-severity-filter", "attention-category-filter", "timeline-kind-filter", "timeline-state-filter", "timeline-model-filter", "relationship-map-preset"].includes(event.target?.id)) supersedeWorkspaceRestore(event.target);
    }, true);
    document.addEventListener("click", (event) => {
      if (["attention-clear-filters", "timeline-clear-filters"].includes(event.target?.id)) supersedeWorkspaceRestore();
    }, true);
    renderWorkspace(current());
  }
  function init() { baseInit(); bindAgentActivity(); initPortalInsights(); initPortalTimeline(); initPortalOutcomes(); initWorkspace(); }

  init();
})();
