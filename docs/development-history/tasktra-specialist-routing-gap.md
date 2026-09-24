# Gap report: project specialist and skill routing in Tasktra

Date: 2026-09-24
Status: observed downstream integration gap; proposed upstream change requires source investigation and independent review.
Origin project: Motif, C:/Users/Matt/Desktop/motif.
Tasktra checkout inspected: C:/Users/Matt/Desktop/Tasktra.

## Problem and impact

A new Motif task requested a character concept for review. It used imagegen directly instead of the configured graphic-designer specialist. The task inspected existing images but did not read the specialist role or art-direction guide. This bypassed the intended specialist workflow despite the specialist configuration being present.

Source task: codex://threads/01a0d321-cab1-7880-814d-a40c80076865
Title: Create Fremen-inspired character.
User prompt: "Make me a new character, and show it to me first for review. I want it to be adventure style version of what a fremen from dune would look like"
The task subsequently acknowledged that it generated directly with imagegen and had not used the graphic-design agent.

This is evidence of a routing miss, not proof of an execution-engine failure or that every Tasktra configuration lacks routing support.

## Verified evidence

- Motif's generated AGENTS.md lists seven core roles; graphic-designer is absent. Its general efficiency guidance does not explicitly route artwork requests to the custom specialist.
- .codex/agents/graphic-designer.toml exists and defines the specialist, model/effort, and integration instructions. Those instructions govern a selected specialist; they did not ensure the parent selected it.
- The Tasktra generated manifest owns AGENTS.md and the seven core roles, but excludes the custom graphic-designer configuration.
- Motif's local mitigation adds AGENTS.override.md and docs/agent-routing.md, covering all eight specialists and matching live skills. The override instructs Codex to read generated AGENTS.md as well.
- A separate reviewer checked that mitigation. Findings about confusing a critique with creation-for-review were corrected: critiques return findings; creation-for-review returns a draft.
- Mechanical checks confirmed all eight role names are covered, referenced files exist, and the mitigation files are outside Tasktra's generated manifest. No Tasktra compile or drift command was run for this mitigation.

The installed Tasktra command was absent from PATH during this investigation. README.md documents an adjacent Tasktra source checkout usable through PYTHONPATH; this is an invocation issue, not evidence that the runtime is absent.

## Likely cause and investigation boundary

The observed configuration separates custom specialist definitions from the generated coordinator instructions. A specialist can exist without a durable project instruction selecting it. Skills and agents are also distinct: choosing an image-generation skill does not dispatch the design specialist.

Inspect current Tasktra support before adding a parallel mechanism. Initial source leads are src/tasktra/compiler.py (_entrypoint and projection/drift ownership), config.py, delegation.py, catalog and pack contracts. These are leads, not a completed architectural diagnosis. The installed Motif manifest identifies Tasktra/catalog version 1.0.0; compare with the current source version.

## Requested outcome

Allow a project to declare how substantive requests select relevant specialists and skills, and compile that policy into the coordinator's entrypoints. Project custom specialists must be discoverable/routable without editing generated files or relying on the user to name them every time.

Use the smallest compatible extension of existing contracts. Consider project-owned routing declarations, catalog/pack merging, stable role references, and explicit handling of externally available skills. Do not embed a copy of every personal/plugin skill in generated instructions.

Required semantics:

- Explicit user instructions win, including model selection and no-subagent requests.
- Artwork concepts, revisions, integration, and critiques route to the design specialist, with distinct output/mutation boundaries.
- Relevant skills supply procedures; specialist delegation remains a separate explicit routing decision.
- Honor explicit-only skill triggers and runtime restrictions on delegation.
- Direct deterministic lookups and trivial mechanical edits remain direct.
- Missing role/model/skill/tool yields a visible, truthful fallback rather than silent omission or fabricated dispatch.
- Preserve role model/effort pins, existing custom files, generated ownership, and regeneration/drift behavior.
- Instructions guide model behavior; do not promise deterministic runtime enforcement unless an actual enforcement mechanism is implemented and tested.

## Acceptance scenarios and checks

1. Create a character for review -> graphic-designer plus imagegen, draft only.
2. Review existing Rill art -> graphic-designer, rubric findings, no generation or mutation.
3. Implement approved art -> export/register/assign and verify actual website surfaces.
4. Fix study behavior -> relevant diagnosis skill and bounded implementation/testing specialists.
5. Review code since main -> applicable code-review workflow and independent reviewers.
6. Write substantive docs -> writing specialist and applicable documentation skill.
7. Find a single asset path -> direct lookup.
8. Explicit no-subagents or unavailable specialist -> documented, truthful fallback.
9. Newly declared custom specialist survives regeneration and appears in applicable routing.
10. Invalid role references, duplicate/conflicting routes, disabled packs, unavailable external skills, and explicit-only skill triggers produce defined, tested behavior.

Add focused tests at the appropriate schema/compiler/delegation boundaries, compatibility tests for projects without custom routing, and regeneration/drift coverage. Run Tasktra's own required checks from its repository instructions. Use a temporary fixture modeled on Motif; do not overwrite Motif's live configuration during upstream development. Independent review should inspect actual code and generated artifacts.

## Risks and non-goals

Avoid blanket delegation, instruction bloat, duplicate competing policies, automatic authority expansion, or silently treating review requests as permission to edit. Do not change model defaults, unrelated Tasktra governance, user progress, or Motif artwork. Publishing/merging/pushing is outside this task unless separately requested.

## Downstream mitigation and handoff

Current local Motif edits: AGENTS.override.md, docs/agent-routing.md, and a README.md pointer. They are uncommitted at report creation. Motif's previous committed artwork update is 9eab0bb74780052953acc083d164606a1e20cf2a. Keep the mitigation until upstream adoption is separately planned and checked.

The user explicitly requested a new Tasktra task using gpt-6-sol at xhigh to fix this gap. This report is evidence and acceptance context; the user's new-task instruction supplies the work authorization. It is not a claim that a formal Tasktra lesson proposal has already been promoted or approved.

## Evidence snapshot hashes (SHA-256)

- `AGENTS.md`: `7ce2b2eb28a2de391434400d7ab0824bea89baa463e0084a5b9dafa48f1c4709`
- `AGENTS.override.md`: `8d7c9bf21b67b813b5a20276833a7d1cd3bac1fc2d81d9cfa93db1aa4bb2419a`
- `docs/agent-routing.md`: `2287f59859268099bdfc40a84c1d8b64f741f73d4358b2d8a605fda5ce9239ee`
- `.codex/agents/graphic-designer.toml`: `be2f7e9d902c6a9c7f5aae60d3c2da9c1d0998aa7574e57d8506393ef13626ec`
- `.tasktra/project.toml`: `7b7bbc14f9cfaa5a9e2b3b800d37dbc706d9e1f234a36b3dcdf796789140e01a`
- `.tasktra/generated/manifest.json`: `83f5c09ea77c0273fb94a0b9607c90fc1061aa0628a7369d4ca5ac05c25af913`
