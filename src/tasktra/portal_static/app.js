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
    activeView: "overview",
    requestInFlight: false,
    retryMs: POLL_MS,
    timer: null,
    controller: null,
    lastError: null,
    modeGeneration: 0,
    liveSnapshot: null,
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
    if (!value) return "No heartbeat";
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
  function renderMetrics(snapshot) {
    const summary = snapshot.summary || {};
    const goals = summary.goals ?? snapshot.goals.length;
    const jobs = summary.jobs ?? snapshot.jobs.length;
    const agents = summary.agents ?? snapshot.agents.length;
    $("metrics").replaceChildren(
      metric(
        "Global active goals",
        number(summary.active_goals ?? 0),
        `${number(goals)} recorded`,
        "accent",
      ),
      metric(
        "Global jobs in motion",
        number(summary.running_jobs ?? 0),
        `${number(jobs)} recorded`,
        "",
      ),
      metric(
        "Global blocked attention",
        number(summary.blocked_jobs ?? 0),
        "Jobs needing a decision",
        summary.blocked_jobs ? "alert" : "",
      ),
      metric(
        "Global agent constellation",
        number(summary.running_agents ?? 0),
        `${number(agents)} recorded`,
        "",
      ),
    );
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
    return ["started", "leased"].includes(agent.state) && !agent.lease_stale;
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
            `${titleCase(event.event_type || "recorded event")} · ${related}`,
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
  function renderJobs(snapshot) {
    const target = $("jobs-body");
    if (isFocusedIn(target)) return;
    const jobs = selectedJobs(snapshot);
    if (!jobs.length) {
      const cell = el(
        "td",
        "record-subtitle",
        state.selectedGoalId
          ? "No jobs match this goal and filter."
          : "No jobs match these filters.",
      );
      cell.colSpan = 6;
      const row = el("tr");
      row.append(cell);
      target.replaceChildren(row);
      return;
    }
    target.replaceChildren(
      ...jobs.map((job) => {
        const row = el("tr");
        const name = el("td");
        name.append(
          textLine("div", "record-title", job.title || job.id),
          textLine("div", "record-subtitle", job.id),
        );
        const stateCell = el("td");
        stateCell.append(status(job.status));
        const owner = el("td", "", job.owner_id || "Unassigned");
        const beat = el(
          "td",
          job.lease_stale ? "stale" : "",
          job.lease_stale ? "Lease stale" : relativeTime(job.heartbeat_at),
        );
        const attempts = el("td", "", number(job.attempt_count ?? 0));
        const action = el("td");
        const button = el("button", "text-button details-trigger", "Details");
        button.type = "button";
        button.dataset.action = "select-job";
        button.dataset.jobId = job.id;
        action.append(button);
        row.append(name, stateCell, owner, beat, attempts, action);
        return row;
      }),
    );
  }
  function renderAgents(snapshot) {
    const target = $("agents-grid");
    if (isFocusedIn(target)) return;
    const agents = snapshot.agents.filter(
      (agent) =>
        !state.selectedGoalId || agent.goal_id === state.selectedGoalId,
    );
    if (!agents.length) {
      target.replaceChildren(
        textLine(
          "p",
          "record-subtitle",
          state.selectedGoalId
            ? "No agents are associated with this goal."
            : "No agents have been recorded.",
        ),
      );
      return;
    }
    target.replaceChildren(
      ...agents.map((agent) => {
        const card = el(
          "article",
          `agent-card ${agentIsActive(agent) ? "agent-active" : ""}`,
        );
        const top = el("div", "agent-card-top");
        top.append(agentAvatar(agent), status(agent.state));
        const detail = el("button", "record-button");
        detail.type = "button";
        detail.dataset.action = "select-agent";
        detail.dataset.agentId = agent.id;
        detail.dataset.agentWorkId = agentKey(agent);
        detail.append(
          textLine("div", "agent-role", agent.role || agent.id),
          textLine(
            "div",
            "agent-meta",
            [agent.model, agent.effort].filter(Boolean).join(" · ") ||
              "Model unavailable",
          ),
        );
        const foot = el("div", "agent-foot");
        foot.append(
          textLine(
            "span",
            "agent-meta",
            agent.lease_stale
              ? "Lease stale"
              : relativeTime(agent.heartbeat_at),
          ),
          textLine(
            "span",
            "agent-meta",
            agent.total_tokens === null || agent.total_tokens === undefined
              ? "Usage unavailable"
              : `${number(agent.total_tokens)} tokens`,
          ),
        );
        card.append(top, detail, foot);
        return card;
      }),
    );
  }
  function detailItem(label, value) {
    const node = el("div", "detail-item");
    node.append(el("b", "", label), el("span", "", value));
    return node;
  }
  function showDetail(type, record, shouldFocus = true) {
    state.selected = {
      type,
      id: type === "agent" ? agentKey(record) : record.id,
    };
    const title =
      type === "goal"
        ? record.title || record.id
        : type === "job"
          ? record.title || record.id
          : record.role || record.id;
    $("detail-kind").textContent = `${titleCase(type)} record`;
    $("detail-title").textContent = title;
    const grid = el("div", "detail-grid");
    if (type === "goal") {
      const budget = record.budget || {};
      const checkpoints = record.checkpoints?.length
        ? record.checkpoints
            .map(
              (checkpoint) =>
                `${checkpoint.id || "Checkpoint"}: ${titleCase(checkpoint.status)}`,
            )
            .join(" · ")
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
        detailItem("Heartbeat", relativeTime(record.heartbeat_at)),
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
    } else {
      grid.append(
        detailItem("State", titleCase(record.state)),
        detailItem("Role", record.role || "Unavailable"),
        detailItem("Agent ID", record.id || "Unavailable"),
        detailItem("Work ID", record.work_id || "Unavailable"),
        detailItem("Model", record.model || "Unavailable"),
        detailItem("Effort", record.effort || "Unavailable"),
        detailItem(
          "Usage",
          record.total_tokens === null || record.total_tokens === undefined
            ? "Unavailable"
            : `${number(record.total_tokens)} tokens`,
        ),
        detailItem("Heartbeat", relativeTime(record.heartbeat_at)),
        detailItem(
          "Lease",
          record.lease_stale
            ? "Stale"
            : record.lease_expires_at
              ? `Expires ${localTime(record.lease_expires_at)}`
              : "Unavailable",
        ),
        detailItem("Provenance", record.provenance || "Unavailable"),
      );
    }
    const content = $("detail-content");
    content.replaceChildren(grid);
    if (record.lease_stale)
      content.append(
        textLine(
          "p",
          "detail-note",
          "The lease is stale, so this record may no longer represent an active worker.",
        ),
      );
    $("detail-panel").hidden = false;
    if (shouldFocus) $("detail-title").focus({ preventScroll: true });
  }
  function refreshDetail(snapshot) {
    if (!state.selected) return;
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
  function render(snapshot) {
    const project = snapshot.project?.name || "This project";
    const runtime = snapshot.runtime || {};
    $("page-title").textContent = project;
    const totalGoals = snapshot.summary?.goals ?? snapshot.goals.length;
    const totalJobs = snapshot.summary?.jobs ?? snapshot.jobs.length;
    const totalAgents = snapshot.summary?.agents ?? snapshot.agents.length;
    $("project-summary").textContent =
      `${number(totalGoals)} recorded goal${totalGoals === 1 ? "" : "s"} · ${number(totalJobs)} recorded job${totalJobs === 1 ? "" : "s"}.`;
    $("last-updated").textContent = snapshot.generated_at
      ? `Last updated ${localTime(snapshot.generated_at)}`
      : "No snapshot timestamp available";
    $("mode-label").textContent = state.demo
      ? "Representative demo · client only"
      : "Live workspace";
    $("footer-state").textContent = state.demo
      ? "Demo data remains in this browser only"
      : runtime.available
        ? "Reading local project state"
        : "Runtime state unavailable";
    $("goal-tab-count").textContent = number(totalGoals);
    $("job-tab-count").textContent = number(totalJobs);
    $("agent-tab-count").textContent = number(totalAgents);
    const selectedGoal = snapshot.goals.find(
      (goal) => goal.id === state.selectedGoalId,
    );
    $("scope-bar").hidden = !selectedGoal;
    $("scope-goal-title").textContent = selectedGoal
      ? selectedGoal.title || selectedGoal.id
      : "";
    renderMetrics(snapshot);
    renderOverviewGoals(snapshot);
    renderOverviewAgents(snapshot);
    renderActivity(snapshot);
    renderGoals(snapshot);
    updateJobFilterOptions(snapshot);
    renderJobs(snapshot);
    renderAgents(snapshot);
    refreshDetail(snapshot);
    const empty = hasNoTrackedWork(snapshot) && !state.demo;
    $("empty-state").hidden = !empty;
    document.querySelectorAll(".metrics,.view-tabs").forEach((node) => {
      node.hidden = empty;
    });
    document.querySelector(".dashboard-grid").hidden =
      empty || state.activeView !== "overview";
    ["overview", "goals", "jobs", "agents"].forEach((name) => {
      $(`view-${name}`).hidden = empty || name !== state.activeView;
    });
    const notices = [...snapshot.warnings.map((warning) => String(warning))];
    if (!state.demo && runtime.emergency_stopped)
      notices.unshift(
        runtime.message ||
          "Tasktra execution is emergency-stopped. Recorded state remains available.",
      );
    else if (!state.demo && runtime.available === false)
      notices.unshift(
        runtime.message ||
          "The Tasktra runtime is unavailable. The portal will retry.",
      );
    else if (state.demo)
      notices.unshift(
        "Representative sample data is visible only in this browser. Exit demo to return to your local project.",
      );
    if (!state.live)
      notices.unshift(
        "Live updates are paused. Use Refresh to request the newest local snapshot.",
      );
    if (!state.lastError)
      setNotice(notices.join(" · "), notices.length ? "warning" : "");
  }
  function switchView(view) {
    state.activeView = view;
    ["overview", "goals", "jobs", "agents"].forEach((name) => {
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
    state.snapshot = normalized;
    state.lastError = null;
    state.retryMs = POLL_MS;
    if (!options.keepDemo) {
      state.demo = false;
      state.liveSnapshot = normalized;
    }
    setConnection("good", state.demo ? "Demo mode" : "Connected");
    render(normalized);
  }
  function normalizeSnapshot(value) {
    const input = value && typeof value === "object" ? value : {};
    return {
      schema_version: input.schema_version || 1,
      generated_at: input.generated_at || null,
      project:
        input.project && typeof input.project === "object"
          ? input.project
          : { name: "This project" },
      runtime:
        input.runtime && typeof input.runtime === "object"
          ? input.runtime
          : {
              available: false,
              emergency_stopped: false,
              message: "Runtime response was incomplete.",
            },
      summary:
        input.summary && typeof input.summary === "object" ? input.summary : {},
      goals: Array.isArray(input.goals) ? input.goals : [],
      jobs: Array.isArray(input.jobs) ? input.jobs : [],
      agents: Array.isArray(input.agents) ? input.agents : [],
      events: Array.isArray(input.events) ? input.events : [],
      warnings: Array.isArray(input.warnings) ? input.warnings : [],
    };
  }
  async function fetchSnapshot(options = {}) {
    const force = Boolean(options.force);
    if ((!state.live && !force) || state.demo || state.requestInFlight) return;
    state.requestInFlight = true;
    state.controller = new AbortController();
    const requestGeneration = state.modeGeneration;
    const timeout = window.setTimeout(() => state.controller.abort(), 8000);
    try {
      const response = await fetch("/api/snapshot", {
        headers: { Accept: "application/json" },
        signal: state.controller.signal,
        cache: "no-store",
      });
      if (!response.ok)
        throw new Error(`Snapshot request returned ${response.status}`);
      const snapshot = await response.json();
      if (requestGeneration !== state.modeGeneration || state.demo) return;
      applySnapshot(snapshot);
    } catch (error) {
      if (requestGeneration !== state.modeGeneration || state.demo) return;
      state.lastError = error;
      const retained = Boolean(state.snapshot);
      setConnection("offline", retained ? "Stale data" : "Disconnected");
      const retryHint = state.live
        ? "Retrying shortly."
        : "Use Refresh to retry.";
      setNotice(
        (retained
          ? "Could not refresh the local snapshot. Showing the last successful data. "
          : "Could not reach the local Tasktra runtime. ") + retryHint,
        "warning",
      );
      state.retryMs = Math.min(Math.round(state.retryMs * 1.8), MAX_BACKOFF_MS);
    } finally {
      window.clearTimeout(timeout);
      state.requestInFlight = false;
      state.controller = null;
      scheduleNext();
    }
  }
  function scheduleNext() {
    window.clearTimeout(state.timer);
    if (state.live && !state.demo)
      state.timer = window.setTimeout(fetchSnapshot, state.retryMs);
  }
  function setDemoControls() {
    $("demo-button").textContent = state.demo ? "Exit demo" : "Explore demo";
    $("demo-header-button").textContent = state.demo ? "Exit demo" : "Demo";
  }
  function exitDemo() {
    state.modeGeneration += 1;
    state.demo = false;
    state.selectedGoalId = null;
    state.selected = null;
    $("detail-panel").hidden = true;
    state.snapshot = state.liveSnapshot || emptySnapshot();
    render(state.snapshot);
    setDemoControls();
  }
  function refreshNow() {
    if (state.demo) exitDemo();
    state.retryMs = POLL_MS;
    fetchSnapshot({ force: true });
  }
  function selectGoal(id) {
    const goal = current().goals.find((item) => item.id === id);
    if (!goal) return;
    state.selectedGoalId = state.selectedGoalId === id ? null : id;
    render(current());
    showDetail("goal", goal);
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
    state.modeGeneration += 1;
    state.demo = true;
    state.selectedGoalId = null;
    state.selected = null;
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
      selectGoal(action.goalId);
      return;
    }
    if (action?.action === "select-job") {
      const record = current().jobs.find((job) => job.id === action.jobId);
      if (record) showDetail("job", record);
      return;
    }
    if (action?.action === "select-agent") {
      const record = current().agents.find(
        (agent) => agentKey(agent) === action.agentWorkId,
      );
      if (record) showDetail("agent", record);
      return;
    }
    const view =
      event.target.closest("[data-view]")?.dataset.view ||
      event.target.closest("[data-switch-view]")?.dataset.switchView;
    if (view) switchView(view);
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
        tabs[next].focus();
        switchView(tabs[next].dataset.view);
      });
    $("job-search").addEventListener("input", () => renderJobs(current()));
    $("job-status-filter").addEventListener("change", () =>
      renderJobs(current()),
    );
    $("clear-job-filters").addEventListener("click", () => {
      $("job-search").value = "";
      $("job-status-filter").value = "all";
      state.selectedGoalId = null;
      render(current());
    });
    $("clear-goal-scope").addEventListener("click", () => {
      state.selectedGoalId = null;
      render(current());
    });
    $("close-detail").addEventListener("click", () => {
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
    $("refresh-button").addEventListener("click", refreshNow);
    $("live-toggle").addEventListener("click", () => {
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
      } else if (state.live && !state.demo) {
        state.retryMs = POLL_MS;
        fetchSnapshot();
      }
    });
  }
  function init() {
    state.live = loadPreference("tasktra-live", true);
    state.motion = loadPreference(
      "tasktra-motion",
      !window.matchMedia("(prefers-reduced-motion: reduce)").matches,
    );
    document.body.classList.toggle("motion-off", !state.motion);
    $("motion-toggle").setAttribute("aria-pressed", String(state.motion));
    $("live-toggle").setAttribute("aria-pressed", String(state.live));
    $("live-toggle").textContent = `Live: ${state.live ? "on" : "paused"}`;
    bind();
    render(emptySnapshot());
    setConnection("", "Connecting");
    if (state.live) fetchSnapshot();
    else {
      setConnection("offline", "Updates paused");
      setNotice(
        "Live updates are paused. Use Refresh to request the newest local snapshot.",
        "warning",
      );
    }
  }
  init();
})();
