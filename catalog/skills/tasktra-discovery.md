---
id = "tasktra-discovery"
title = "Discover and plan a project or large feature"
family = "planning"
---
# Discover and plan a project or large feature

Use when a human has a rough product idea or a substantial feature request and wants an implementation-ready plan. This skill plans; it does not create a goal, dispatch workers, or change application code.

## Choose the mode

- **New project:** turn the idea into a smallest useful first release and clearly list later work that is out of scope.
- **Existing project or large feature:** inspect the relevant product documents, project profile, architecture, and affected code first. Plan the change as a delta from the current system.

## Discovery

1. Start with the desired user outcome. State any reasonable assumptions, then ask short grouped questions only where the answers would change the product, architecture, safety, cost, schedule, or definition of done. Cover users, primary flows, success measures, constraints, data and privacy, integrations, rollout, and explicit exclusions as applicable.
2. Keep unresolved decisions visible. Offer a clearly labelled default when it is safe and useful; never present a default as a human decision.
3. When there is enough information, create `docs/plans/<plan-id>.md`. Use a lowercase hyphenated plan ID that describes the outcome. This Markdown file is project-owned documentation and should normally be committed.

## Plan document

Write a human-readable plan with these sections, omitting only sections that truly do not apply:

1. Outcome, users, and success measures.
2. In scope, explicitly out of scope, assumptions, and unresolved decisions.
3. User journeys and independently testable acceptance criteria.
4. Domain concepts, data, interfaces, architecture choices, and important trade-offs.
5. Delivery risks: privacy, security, accessibility, performance, dependencies, migration, rollout, and rollback where relevant.
6. Ordered work items. For each item, include its purpose, affected area, dependency, responsible Tasktra role, and observable verification.
7. Milestones and release criteria.

For an existing project, cite the files or existing behavior that shaped the plan. Do not invent an architecture that conflicts with verified project evidence.

## Review and handoff

1. Present the completed plan and the decisions that still need a human answer. Stop for review; the plan itself grants no implementation authority.
2. After the human explicitly approves the plan and authorizes work, invoke `tasktra-goal`. Use the approved plan to define the goal, acceptance criteria, scope, exclusions, budgets, checkpoints, required validation, and stop conditions.
3. Keep the approved Markdown plan as the human explanation. The goal, leases, work attempts, handoffs, and evidence live in Tasktra's local runtime database. Use `tasktra-run` only after the goal is active.

If new information makes the approved plan materially wrong, return to Discovery, update the Markdown plan, and request a new approval before widening the goal.
