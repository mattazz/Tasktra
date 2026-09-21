"""Safe deterministic primitives for Codex and terminal callers."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import sysconfig
from typing import Any, Sequence

from . import __version__
from .adoption import preview_initialization
from .benchmarking import BenchmarkObservation, BenchmarkPlan, compare_observations
from .compiler import CatalogError, catalog_digest, check_drift, compile_catalog, load_catalog, write_projection
from .ecosystem import preflight_packs, preview_pack_migrations, recommend_packs
from .config import ConfigError, config_path, initialize_project, load_project_config
from .delegation import DelegationError, delegation_plan, projection_overrides
from .handoffs import MAX_HANDOFF_BYTES, HandoffError, load_handoff, validate_handoff
from .lessons import LessonError, LessonProposalStore
from .lifecycle import LifecycleError, preview_adoption, preview_upgrade
from .manifest import (
    ManifestError,
    build_generated_manifest,
    build_lockfile,
    read_lockfile,
    read_manifest,
    sha256_bytes,
    write_lockfile,
    write_manifest,
)
from .operations import MAX_AUDIT_EXPORT, MAX_DETAIL_LIMIT, export_audit, operational_status
from .migrations import MigrationError
from .contracts import validate_named
from .providers import (
    MAX_PROVIDER_JSON_BYTES,
    OperationDescriptor,
    ProviderError,
    ProviderHealth,
    ProviderRegistry,
    load_bounded_provider_json,
)
from .release import audit_release
from .scheduling import SchedulerError, load_scheduler_health, load_schedule_resume, preview_schedule, reserve_schedule_resume
from .provider_adapters import BoundedArgvRunner, GitHubCliAdapter, JiraConnectorAdapter
from .provider_execution import ProviderEffectExecutor
from .jira_sync import JiraSyncError, build_sync_plan
from .state import SCHEMA_VERSION as STATE_SCHEMA_VERSION, StateError, StateStore
from .telemetry import TelemetryError, TelemetryStore
from .upgrades import UpgradeError, apply_upgrade, rollback_upgrade, upgrade_plan_digest
from .autonomy import AutonomyStore
from .authority import (
    load_authority_envelope,
    load_transition_approval,
    transition_approval_subject_sha256,
)
from .validation import ValidationError, run_validations, validation_plan
from .workitems import WorkItemError, WorkItemStore
from .workspace import WorkspaceRequest, assess_workspace, recommend_workspace
from .workflow import (
    MAX_WORKFLOW_BYTES,
    WorkflowError,
    accept_handoff,
    is_workflow_complete,
    load_workflow,
    new_workflow,
    workflow_completion_token,
)


def _root(value: str) -> Path:
    return Path(value).expanduser().resolve()


def _emit(value: Any, stream: Any = None) -> None:
    destination = sys.stdout if stream is None else stream
    json.dump(value, destination, indent=2, sort_keys=True)
    destination.write("\n")


def _store(root: Path) -> StateStore:
    config = load_project_config(root)
    return StateStore(config.database_path(root))


_MAX_RUNTIME_JSON_BYTES = 64 * 1024


class _StrictArgumentParser(argparse.ArgumentParser):
    """Disable long-option abbreviation for every command and subcommand."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)


def _autonomy(root: Path) -> AutonomyStore:
    config = load_project_config(root)
    return AutonomyStore(config.database_path(root))


def build_parser() -> argparse.ArgumentParser:
    parser = _StrictArgumentParser(
        prog="tasktra",
        description="Tasktra workflow primitives; project-owned configuration lives in .tasktra/project.toml",
    )
    parser.add_argument("--version", action="version", version=f"tasktra {__version__}")
    subcommands = parser.add_subparsers(dest="command", required=True)

    init = subcommands.add_parser("init", help="Create a project profile without overwriting existing files")
    init.add_argument("--root", default=".")
    init.add_argument("--name")
    init.add_argument("--preview", "--dry-run", action="store_true", help="Inspect adoption without writing files")
    init.add_argument("--apply", action="store_true", help="Apply the previewed project profile")
    init.add_argument("--verbose", action="store_true", help="Include per-file instruction inventory and hashes")

    bootstrap = subcommands.add_parser(
        "bootstrap", help="Create a local runtime while preserving an existing project profile"
    )
    bootstrap.add_argument("--root", default=".")
    bootstrap.add_argument("--name", help="Name for a newly created project profile")

    status = subcommands.add_parser("status", help="Show local project and runtime status")
    status.add_argument("--root", default=".")
    status.add_argument("--goal-id")
    status.add_argument("--detail-limit", type=int, default=0)

    doctor = subcommands.add_parser("doctor", help="Inspect configuration and local runtime availability")
    doctor.add_argument("--root", default=".")

    validate = subcommands.add_parser("validate", help="Preview or run project validation commands")
    validate.add_argument("--root", default=".")
    validate.add_argument("--run", action="store_true", help="Execute the previewed commands")
    validate.add_argument("--timeout", type=int, default=300, help="Per-command timeout in seconds")

    work_item = subcommands.add_parser("work-item", help="Manage local Markdown work items")
    work_item.add_argument("--root", default=".")
    work_item_commands = work_item.add_subparsers(dest="work_item_command", required=True)
    work_item_create = work_item_commands.add_parser("create", help="Create a local work item")
    work_item_create.add_argument("item_id")
    work_item_create.add_argument("title")
    work_item_create.add_argument("--body", default="")
    work_item_create.add_argument("--status", default="planned")
    work_item_create.add_argument("--goal-id")
    work_item_create.add_argument("--label", action="append", default=[])
    work_item_commands.add_parser("list", help="List local work items")
    work_item_show = work_item_commands.add_parser("show", help="Show one local work item")
    work_item_show.add_argument("item_id")
    work_item_update = work_item_commands.add_parser("update", help="Update one local work item")
    work_item_update.add_argument("item_id")
    work_item_update.add_argument("--expected-version", type=int, required=True)
    work_item_update.add_argument("--title")
    work_item_update.add_argument("--body")
    work_item_update.add_argument("--status")
    work_item_update.add_argument("--goal-id")
    work_item_update.add_argument("--label", action="append")
    work_item_update.add_argument("--workflow", help="Completed workflow JSON required for status done")

    handoff = subcommands.add_parser("handoff", help="Validate structured handoff envelopes")
    handoff_commands = handoff.add_subparsers(dest="handoff_command", required=True)
    handoff_template = handoff_commands.add_parser("template", help="Print a valid partial v1 handoff template")
    handoff_template.add_argument("--goal-id", required=True)
    handoff_template.add_argument("--work-unit-id")
    handoff_template.add_argument("--role", default="implementer")
    handoff_template.add_argument("--actor-id", default="unassigned")
    handoff_template.add_argument("--handoff-id", default="handoff-template")
    handoff_validate = handoff_commands.add_parser("validate", help="Validate a handoff JSON file")
    handoff_validate.add_argument("path")

    workflow = subcommands.add_parser("workflow", help="Create, validate, or advance implementation workflows")
    workflow_commands = workflow.add_subparsers(dest="workflow_command", required=True)
    workflow_create = workflow_commands.add_parser("create", help="Print a new implementation workflow state")
    workflow_create.add_argument("--goal-id", required=True)
    workflow_create.add_argument("--work-unit-id")
    workflow_validate = workflow_commands.add_parser("validate", help="Validate a workflow JSON file")
    workflow_validate.add_argument("path")
    workflow_accept = workflow_commands.add_parser("accept", help="Accept one validated handoff into a workflow")
    workflow_accept.add_argument("workflow_path")
    workflow_accept.add_argument("handoff_path")

    workspace = subcommands.add_parser("workspace", help="Inspect Git state and recommend safe work placement")
    workspace.add_argument("--root", default=".")
    workspace.add_argument("--read-only", action="store_true")
    workspace.add_argument("--change-scope", choices=["small", "substantial"], default="small")
    workspace.add_argument("--concurrent-workers", type=int, default=1)
    workspace.add_argument("--paths-known-disjoint", action="store_true")
    workspace.add_argument("--require-isolation", action="store_true")

    capabilities = subcommands.add_parser("capabilities", help="Report local and optional capabilities")
    capabilities.add_argument("--root", default=".")
    capabilities.add_argument(
        "--provider-health",
        help="one-invocation bounded provider-health report supplied by a trusted host",
    )

    schedule = subcommands.add_parser(
        "schedule",
        help="Build a read-only, durable resume plan for optional scheduler capabilities",
    )
    schedule.add_argument("--root", default=".")
    schedule_commands = schedule.add_subparsers(dest="schedule_command", required=True)
    schedule_preview = schedule_commands.add_parser("preview", help="Preview only; does not create or manage a scheduler")
    schedule_preview.add_argument("--goal-id", required=True)
    schedule_preview.add_argument("--work-unit-id", required=True)
    schedule_preview.add_argument("--envelope-sha256", required=True)
    schedule_preview.add_argument("--checkpoint")
    schedule_preview.add_argument("--cadence", default="on-demand")
    schedule_preview.add_argument("--notification-intent", choices=("none", "on-failure", "always"), default="on-failure")
    schedule_preview.add_argument("--adapter", choices=("auto", "codex-scheduled-tasks", "ci", "local-runner", "manual"), default="auto")
    schedule_preview.add_argument("--scheduler-health", help="bounded non-authoritative host scheduler health JSON")
    schedule_preview.add_argument("--performer-id", required=True)
    schedule_preview.add_argument("--repository", required=True)
    schedule_preview.add_argument("--revision", required=True)
    schedule_preview.add_argument("--branch", required=True)
    schedule_preview.add_argument("--workspace", required=True)
    schedule_preview.add_argument("--lease-seconds", type=int, default=300)
    schedule_preview.add_argument("--token-reservation", type=int, default=0)
    schedule_resume = schedule_commands.add_parser("resume")
    schedule_resume.add_argument("plan")
    schedule_resume.add_argument("--actor", required=True)
    schedule_resume.add_argument("--lease-token-env", default="TASKTRA_LEASE_TOKEN")

    release = subcommands.add_parser("release", help="Inspect deterministic release evidence without writing or approving")
    release.add_argument("--root", default=".")
    release_commands = release.add_subparsers(dest="release_command", required=True)
    release_audit = release_commands.add_parser("audit", help="Fail closed unless every release gate has durable evidence")
    release_audit.add_argument("--expected-version", default="1.0.0")

    compile_command = subcommands.add_parser("compile", help="Compile or check managed agent projections")
    compile_command.add_argument("--root", default=".")
    compile_command.add_argument("--catalog", help="Canonical catalog directory; defaults to <root>/catalog")
    compile_command.add_argument("--trust-catalog", action="store_true", help="Explicitly trust an external catalog as agent instructions")
    compile_command.add_argument("--check", action="store_true", help="Report drift without writing")
    compile_command.add_argument("--force", action="store_true", help="Replace changed managed files after review")
    compile_command.add_argument("--capability", action="append", default=[], help="Credential-free available capability id")
    compile_command.add_argument("--trust-executable", action="append", default=[], help="Trusted pack token id@version:sha256")
    compile_command.add_argument(
        "--prune-stale",
        action="store_true",
        help="Delete unchanged stale managed outputs after hash verification",
    )

    delegation = subcommands.add_parser("delegation", help="Create a read-only Codex-host delegation plan")
    delegation.add_argument("--root", default=".")
    delegation_commands = delegation.add_subparsers(dest="delegation_command", required=True)
    delegation_plan_command = delegation_commands.add_parser("plan", help="Resolve a bounded worker brief without dispatching")
    delegation_plan_command.add_argument("request")
    delegation_plan_command.add_argument("--handoff")

    packs = subcommands.add_parser("packs", help="Preview ecosystem pack recommendations and activation")
    packs.add_argument("--root", default=".")
    pack_commands = packs.add_subparsers(dest="pack_command", required=True)
    recommend = pack_commands.add_parser("recommend", help="Inspect bounded local evidence without writing")
    recommend.add_argument("--catalog")
    recommend.add_argument("--trust-catalog", action="store_true")
    recommend.add_argument("--max-files", type=int, default=256)
    recommend.add_argument("--max-depth", type=int, default=4)
    preflight = pack_commands.add_parser("preflight", help="Report capability and trust blockers")
    preflight.add_argument("--catalog")
    preflight.add_argument("--trust-catalog", action="store_true")
    preflight.add_argument("--pack", action="append", default=[])
    preflight.add_argument("--capability", action="append", default=[])
    preflight.add_argument("--trust-executable", action="append", default=[], help="Trusted pack token id@version:sha256")
    migrations = pack_commands.add_parser("migration-preview", help="Preview declared pack migrations")
    migrations.add_argument("--catalog")
    migrations.add_argument("--trust-catalog", action="store_true")
    migrations.add_argument("--pack", action="append", default=[])
    migrations.add_argument("--trust-executable", action="append", default=[], help="Trusted pack token id@version:sha256")

    jira_sync = subcommands.add_parser("jira-sync", help="Plan an optional Jira status transition without contacting Jira")
    jira_sync.add_argument("--root", default=".")
    jira_sync_commands = jira_sync.add_subparsers(dest="jira_sync_command", required=True)
    jira_sync_plan = jira_sync_commands.add_parser("plan", help="Build a closed Jira transition request for one Tasktra lifecycle event")
    jira_sync_plan.add_argument("--event", choices=("claimed", "review-ready", "completed"), required=True)
    jira_sync_plan.add_argument("--issue", required=True, help="Linked Jira issue key, for example PROJ-123")
    jira_sync_plan.add_argument("--goal-id", required=True)
    jira_sync_plan.add_argument("--work-unit-id", required=True)

    adopt = subcommands.add_parser("adopt", help="Preview Tasktra adoption without changing the project")
    adopt.add_argument("--root", default=".")
    adopt.add_argument("--catalog")
    adopt.add_argument("--trust-catalog", action="store_true")
    adopt.add_argument("--pack", action="append", default=[])
    adopt.add_argument("--capability", action="append", default=[])
    adopt.add_argument("--trust-executable", action="append", default=[])

    upgrade = subcommands.add_parser("upgrade", help="Preview, apply, or roll back an exact Tasktra upgrade")
    upgrade.add_argument("--root", default=".")
    upgrade_commands = upgrade.add_subparsers(dest="upgrade_command", required=True)
    upgrade_preview = upgrade_commands.add_parser("preview", help="Create a read-only, digest-bound upgrade plan")
    upgrade_preview.add_argument("--catalog")
    upgrade_preview.add_argument("--trust-catalog", action="store_true")
    upgrade_preview.add_argument("--pack", action="append", default=[])
    upgrade_preview.add_argument("--capability", action="append", default=[])
    upgrade_preview.add_argument("--trust-executable", action="append", default=[])
    upgrade_apply = upgrade_commands.add_parser("apply", help="Apply the current exact plan under durable authority")
    upgrade_apply.add_argument("--catalog")
    upgrade_apply.add_argument("--trust-catalog", action="store_true")
    upgrade_apply.add_argument("--pack", action="append", default=[])
    upgrade_apply.add_argument("--capability", action="append", default=[])
    upgrade_apply.add_argument("--trust-executable", action="append", default=[])
    upgrade_apply.add_argument("--plan-sha256", required=True)
    upgrade_apply.add_argument("--goal-id", required=True)
    upgrade_apply.add_argument("--work-unit-id", required=True)
    upgrade_apply.add_argument("--envelope-sha256", required=True)
    upgrade_apply.add_argument("--actor", required=True)
    upgrade_apply.add_argument("--idempotency-key", required=True)
    upgrade_apply.add_argument("--confirm", action="store_true")
    upgrade_apply.add_argument("--allow-network", action="store_true")
    upgrade_apply.add_argument("--timeout", type=int, default=300)
    upgrade_rollback = upgrade_commands.add_parser("rollback", help="Restore an exact pre-upgrade snapshot under durable authority")
    upgrade_rollback.add_argument("--snapshot-plan-sha256", required=True)
    upgrade_rollback.add_argument("--before-sha256", required=True)
    upgrade_rollback.add_argument("--goal-id", required=True)
    upgrade_rollback.add_argument("--work-unit-id", required=True)
    upgrade_rollback.add_argument("--envelope-sha256", required=True)
    upgrade_rollback.add_argument("--actor", required=True)
    upgrade_rollback.add_argument("--idempotency-key", required=True)
    upgrade_rollback.add_argument("--confirm", action="store_true")

    telemetry = subcommands.add_parser("telemetry", help="Inspect, opt in to, or explicitly export local bounded telemetry")
    telemetry.add_argument("--root", default=".")
    telemetry_commands = telemetry.add_subparsers(dest="telemetry_command", required=True)
    telemetry_commands.add_parser("status", help="Report local records without enabling collection")
    telemetry_record = telemetry_commands.add_parser("record", help="Append one closed-schema record with explicit opt-in")
    telemetry_record.add_argument("path", help="bounded telemetry record JSON")
    telemetry_record.add_argument("--enable", action="store_true", help="explicitly enable this local append")
    telemetry_export = telemetry_commands.add_parser("export", help="Write an explicit sanitized project-local export")
    telemetry_export.add_argument("destination", help="project-relative destination")

    benchmark = subcommands.add_parser("benchmark", help="Compare measured representative observations")
    benchmark.add_argument("baseline", help="bounded baseline JSON array")
    benchmark.add_argument("candidate", help="bounded candidate JSON array")
    benchmark.add_argument("--scenario", action="append", default=[])

    lesson = subcommands.add_parser("lesson", help="Create and independently review durable lesson proposals")
    lesson.add_argument("--root", default=".")
    lesson_commands = lesson.add_subparsers(dest="lesson_command", required=True)
    lesson_create = lesson_commands.add_parser("create")
    lesson_create.add_argument("path", help="bounded proposal input JSON")
    lesson_commands.add_parser("list")
    lesson_show = lesson_commands.add_parser("show"); lesson_show.add_argument("proposal_id")
    for name in ("review", "approve", "reject"):
        transition = lesson_commands.add_parser(name)
        transition.add_argument("proposal_id")
        transition.add_argument("--expected-version", type=int, required=True)
        transition.add_argument("--actor", required=True)
        if name in {"approve", "reject"}:
            transition.add_argument("--reason", required=True)
    lesson_promote = lesson_commands.add_parser("promotion-preview")
    lesson_promote.add_argument("proposal_id")

    goal = subcommands.add_parser("goal", help="Create or inspect durable goals")
    goal.add_argument("--root", default=".")
    goal_commands = goal.add_subparsers(dest="goal_command", required=True)
    create = goal_commands.add_parser("create", help="Create an approved-scope goal record")
    create.add_argument("title")
    create.add_argument("description")
    create.add_argument("--id", dest="goal_id")
    create.add_argument("--priority", type=int, default=0)
    create.add_argument("--budget", type=int)
    create.add_argument("--acceptance", action="append", default=[])
    listing = goal_commands.add_parser("list", help="List goals")
    listing.add_argument("--status", choices=["planned", "active", "paused", "blocked", "complete", "stopped"])
    show = goal_commands.add_parser("show", help="Show one goal and budget")
    show.add_argument("goal_id")

    state = subcommands.add_parser("state", help="Inspect or explicitly migrate runtime state")
    state.add_argument("--root", default=".")
    state_commands = state.add_subparsers(dest="state_command", required=True)
    migrate = state_commands.add_parser("migrate", help="Preview or apply a runtime migration")
    choice = migrate.add_mutually_exclusive_group(required=True)
    choice.add_argument("--preview", action="store_true")
    choice.add_argument("--apply", action="store_true")
    attest = state_commands.add_parser("attest-ledger", help="Human-attest the complete reviewed ledger after migration")
    attest.add_argument("--human-actor", required=True)

    runtime = subcommands.add_parser("runtime", help="Run explicit Stage 3 state controls")
    runtime.add_argument("--root", default=".")
    runtime_commands = runtime.add_subparsers(dest="runtime_command", required=True)
    for name in ("emergency-stop", "clear-emergency-stop"):
        item = runtime_commands.add_parser(name)
        item.add_argument("--actor", required=True)
        if name == "emergency-stop": item.add_argument("--reason", required=True)
        else: item.add_argument("--actor-kind", choices=["human"], required=True)

    contract = subcommands.add_parser("contract", help="Store a bounded authority envelope")
    contract.add_argument("--root", default="."); contract.add_argument("goal_id"); contract.add_argument("path"); contract.add_argument("--actor", required=True)
    approval = subcommands.add_parser("approval", help="Record or revoke protected transition approval")
    approval.add_argument("--root", default="."); approval_commands = approval.add_subparsers(dest="approval_command", required=True)
    approval_record = approval_commands.add_parser("record"); approval_record.add_argument("path"); approval_record.add_argument("--human-actor"); approval_record.add_argument("--codex-user-message", help="exact explicit human approval message received through Codex")
    approval_revoke = approval_commands.add_parser("revoke"); approval_revoke.add_argument("approval_id"); approval_revoke.add_argument("--actor", required=True)
    approval_repair = approval_commands.add_parser("repair-scope", help="Human-repair an unsealed migrated approval scope")
    approval_repair.add_argument("approval_id"); approval_repair.add_argument("path"); approval_repair.add_argument("--human-actor", required=True)

    for name in ("activate", "resume", "complete"):
        item = goal_commands.add_parser(name); item.add_argument("goal_id"); item.add_argument("--actor", required=True); item.add_argument("--envelope-sha256", required=True)
    for name in ("pause", "stop"):
        item = goal_commands.add_parser(name); item.add_argument("goal_id"); item.add_argument("--actor", required=True)

    work = subcommands.add_parser("work", help="Create and safely execute work units")
    work.add_argument("--root", default="."); work_commands = work.add_subparsers(dest="work_command", required=True)
    work_create = work_commands.add_parser("create"); work_create.add_argument("goal_id"); work_create.add_argument("title"); work_create.add_argument("--id"); work_create.add_argument("--scope", required=True, help="bounded closed JSON scope file"); work_create.add_argument("--checkpoint", help="contract checkpoint identifier")
    assign_checkpoint = work_commands.add_parser("assign-checkpoint", help="Explicitly bind an unleased legacy work unit to a contract checkpoint")
    assign_checkpoint.add_argument("work_unit_id"); assign_checkpoint.add_argument("checkpoint_id"); assign_checkpoint.add_argument("--human-actor", required=True)
    claim = work_commands.add_parser("claim"); claim.add_argument("goal_id"); claim.add_argument("--actor", required=True); claim.add_argument("--envelope-sha256", required=True); claim.add_argument("--lease-seconds", type=int, default=300); claim.add_argument("--token-reservation", type=int, default=0); claim.add_argument("--repository", required=True); claim.add_argument("--revision", required=True); claim.add_argument("--branch", required=True); claim.add_argument("--workspace", required=True); claim.add_argument("--lease-token-env", default="TASKTRA_LEASE_TOKEN")
    for name in ("heartbeat", "finish"):
        item = work_commands.add_parser(name); item.add_argument("attempt_id"); item.add_argument("--actor", required=True); item.add_argument("--lease-token-env", default="TASKTRA_LEASE_TOKEN")
        if name == "heartbeat": item.add_argument("--lease-seconds", type=int, default=300)
        else: item.add_argument("--outcome", required=True); item.add_argument("--tokens-consumed", type=int, default=0); item.add_argument("--workflow"); item.add_argument("--evidence-json")
    recover = work_commands.add_parser("recover"); recover.add_argument("--goal-id")
    requeue = work_commands.add_parser("requeue", help="Evidently resume blocked or approval-required work")
    requeue.add_argument("work_unit_id"); requeue.add_argument("--actor", required=True); requeue.add_argument("--envelope-sha256", required=True); requeue.add_argument("--evidence-json", required=True)
    evidence = subcommands.add_parser("acceptance-evidence", help="Record bounded evidence for an acceptance criterion")
    evidence.add_argument("--root", default="."); evidence.add_argument("goal_id"); evidence.add_argument("criterion_id"); evidence.add_argument("--actor", required=True); evidence.add_argument("--envelope-sha256", required=True); evidence.add_argument("--evidence", required=True)
    effect = subcommands.add_parser("effect", help="Prepare, record, or inspect an idempotent effect")
    effect.add_argument("--root", default="."); effect_commands = effect.add_subparsers(dest="effect_command", required=True)
    prepare = effect_commands.add_parser("prepare"); prepare.add_argument("key"); prepare.add_argument("goal_id"); prepare.add_argument("--work-unit-id"); prepare.add_argument("--class", dest="effect_class", required=True); prepare.add_argument("--operation", required=True); prepare.add_argument("--request", required=True); prepare.add_argument("--envelope-sha256", required=True); prepare.add_argument("--actor", required=True)
    receipt = effect_commands.add_parser("receipt"); receipt.add_argument("key"); receipt.add_argument("--outcome", required=True); receipt.add_argument("--evidence", required=True); receipt.add_argument("--actor", required=True); receipt.add_argument("--before-sha256"); receipt.add_argument("--after-sha256")
    inspect = effect_commands.add_parser("inspect"); inspect.add_argument("key")
    provider_prepare = effect_commands.add_parser("provider-prepare", help="Durably prepare a protocol-v2 provider effect; does not invoke a provider")
    provider_prepare.add_argument("key"); provider_prepare.add_argument("--goal-id", required=True); provider_prepare.add_argument("--work-unit-id", required=True); provider_prepare.add_argument("--descriptor", required=True, help="closed protocol-v2 operation descriptor JSON file"); provider_prepare.add_argument("--request", required=True, help="bounded provider request JSON file"); provider_prepare.add_argument("--work-attempt-id", required=True); provider_prepare.add_argument("--envelope-sha256", required=True); provider_prepare.add_argument("--actor", required=True); provider_prepare.add_argument("--lease-token-env", default="TASKTRA_LEASE_TOKEN")
    provider_execute = effect_commands.add_parser("provider-execute", help="Execute one prepared provider effect through a ledger-backed configured adapter")
    provider_execute.add_argument("key"); provider_execute.add_argument("--descriptor", required=True, help="the exact prepared protocol-v2 operation descriptor JSON file"); provider_execute.add_argument("--actor", required=True); provider_execute.add_argument("--lease-token-env", default="TASKTRA_LEASE_TOKEN")
    provider_reconcile = effect_commands.add_parser("provider-reconcile", help="Record a bounded provider reconciliation observation")
    provider_reconcile.add_argument("key"); provider_reconcile.add_argument("--resolution", choices=("applied", "conflict"), required=True); provider_reconcile.add_argument("--observation", required=True, help="bounded reconciliation observation JSON file"); provider_reconcile.add_argument("--actor", required=True)
    audit = subcommands.add_parser("audit", help="Verify or export the runtime audit chain")
    audit.add_argument("--root", default=".")
    audit_commands = audit.add_subparsers(dest="audit_command", required=True)
    audit_verify = audit_commands.add_parser("verify")
    audit_verify.add_argument("--limit", type=int, default=8)
    audit_export = audit_commands.add_parser("export")
    audit_export.add_argument("--limit", type=int, default=50)
    audit_export.add_argument("--after-sequence", type=int, default=0)
    audit_export.add_argument("--goal-id")
    return parser


def _init(args: argparse.Namespace) -> dict[str, Any]:
    root = _root(args.root)
    preview = preview_initialization(root, name=args.name)
    if args.preview or not args.apply:
        result = preview.as_dict()
        instructions = result.pop("instructions")
        result["instruction_summary"] = _instruction_summary(preview)
        if args.verbose:
            result["instructions"] = instructions
        return result
    destination = initialize_project(root, name=args.name)
    state = _store(root)
    state.migrate()
    result = {
        "ok": True,
        "action": "init",
        "config": str(destination),
        "database": str(state.path),
        "instruction_summary": _instruction_summary(preview),
        "preserved_instruction_count": len(preview.instructions),
    }
    if args.verbose:
        result["instructions"] = [item.as_dict() for item in preview.instructions]
    return result


def _bootstrap(args: argparse.Namespace) -> dict[str, Any]:
    """Make a checkout runnable without treating its committed profile as disposable.

    ``init`` remains deliberately conflict-safe: it creates a project profile once
    and refuses to replace it.  A source checkout already has that project-owned
    profile, but its ignored SQLite runtime is absent in a fresh clone.  Bootstrap
    preserves the profile byte-for-byte and initializes only that local runtime.
    """
    root = _root(args.root)
    profile = config_path(root)
    if profile.exists():
        # Validate before writing runtime state so a malformed preserved profile
        # cannot be mistaken for a bootstrap target.
        load_project_config(root)
        config_action = "preserve"
    else:
        profile = initialize_project(root, name=args.name)
        config_action = "create"
    state = _store(root)
    schema_version = state.migrate()
    return {
        "ok": True,
        "action": "bootstrap",
        "config": str(profile),
        "config_action": config_action,
        "database": str(state.path),
        "runtime_schema": schema_version,
    }


def _status(args: argparse.Namespace) -> dict[str, Any]:
    root = _root(args.root)
    config = load_project_config(root)
    state = StateStore(config.database_path(root))
    runtime = operational_status(state, goal_id=args.goal_id, detail_limit=args.detail_limit)
    return {"ok": True, "project": {"name": config.name, "packs": list(config.enabled_packs), "concurrency_limit": config.concurrency_limit}, "runtime": runtime}


def _audit(args: argparse.Namespace) -> dict[str, Any]:
    store = _store(_root(args.root))
    if args.audit_command == "verify":
        return {"ok": True, "audit": store.verify_audit(limit=args.limit)}
    return {
        "ok": True,
        "audit": export_audit(
            store,
            after_sequence=args.after_sequence,
            limit=args.limit,
            goal_id=args.goal_id,
        ),
    }


def _doctor(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    root = _root(args.root)
    checks: list[dict[str, Any]] = [{"name": "root", "ok": root.is_dir(), "detail": str(root)}]
    path = config_path(root)
    if not path.is_file():
        checks.append({"name": "configuration", "ok": False, "detail": f"missing {path}"})
        return {"ok": False, "checks": checks}, 1
    try:
        config = load_project_config(root)
        checks.append({"name": "configuration", "ok": True, "detail": str(path)})
        database = config.database_path(root)
        checks.append({"name": "runtime_database", "ok": database.exists(), "detail": str(database)})
        if database.exists():
            version = StateStore(database).inspect_schema_version()
            current = version == STATE_SCHEMA_VERSION
            detail = (
                f"schema {version} is current"
                if current
                else f"schema {version} requires explicit migration to {STATE_SCHEMA_VERSION}"
            )
            checks.append({"name": "runtime_state", "ok": current, "detail": detail})
    except (ConfigError, StateError, OSError) as error:
        checks.append({"name": "configuration", "ok": False, "detail": str(error)})
    healthy = all(check["ok"] for check in checks)
    return {"ok": healthy, "checks": checks}, 0 if healthy else 1


def _validate_project(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    root = _root(args.root)
    config = load_project_config(root)
    results = (
        run_validations(root, config.validation_commands, timeout_seconds=args.timeout)
        if args.run
        else validation_plan(config.validation_commands)
    )
    passed = all(item.status == "passed" for item in results) if args.run else True
    return {
        "ok": passed,
        "action": "validation-run" if args.run else "validation-preview",
        "commands": [item.as_dict() for item in results],
    }, 0 if passed else 1


def _work_item(args: argparse.Namespace) -> dict[str, Any]:
    store = WorkItemStore(_root(args.root))
    if args.work_item_command == "create":
        item = store.create(
            item_id=args.item_id,
            title=args.title,
            body=args.body,
            status=args.status,
            goal_id=args.goal_id,
            labels=args.label,
        )
        return {"ok": True, "action": "work-item-create", "item": item.as_dict()}
    if args.work_item_command == "list":
        return {"ok": True, "action": "work-item-list", "items": [item.as_dict() for item in store.list()]}
    if args.work_item_command == "show":
        return {"ok": True, "action": "work-item-show", "item": store.read(args.item_id).as_dict()}
    if args.work_item_command == "update":
        changes = {
            name: value
            for name, value in {
                "title": args.title,
                "body": args.body,
                "status": args.status,
                "goal_id": args.goal_id,
                "labels": args.label,
            }.items()
            if value is not None
        }
        if not changes:
            raise WorkItemError("work-item update requires at least one changed field")
        completion_workflow = _read_bounded_json_contract(args.workflow, MAX_WORKFLOW_BYTES, load_workflow) if args.workflow else None
        item = store.update(
            args.item_id,
            expected_version=args.expected_version,
            completion_workflow=completion_workflow,
            **changes,
        )
        return {"ok": True, "action": "work-item-update", "item": item.as_dict()}
    raise AssertionError(f"Unhandled work-item command: {args.work_item_command}")


def _handoff(args: argparse.Namespace) -> dict[str, Any]:
    if args.handoff_command == "template":
        envelope = validate_handoff({
            "kind": "tasktra.handoff",
            "version": 1,
            "handoff_id": args.handoff_id,
            "source": {"goal_id": args.goal_id, "work_unit_id": args.work_unit_id},
            "producer": {"role": args.role, "actor_id": args.actor_id},
            "human_summary": "Partial handoff template; complete it with bounded evidence before advancing.",
            "status": {"state": "partial", "summary": "Template only; no work has been performed."},
            "verified_facts": [],
            "inferences": [],
            "changed_paths": [],
            "validation_results": [],
            "evidence_refs": [],
            "blockers": [],
            "downstream_brief": {
                "objective": "Complete the bounded work and record evidence.",
                "context": [],
                "constraints": ["Do not infer authority from this template."],
                "recommended_next_steps": ["Replace template text with verified handoff evidence."],
            },
            "requested_actions": [],
        })
        return {"ok": True, "action": "handoff-template", "handoff": envelope}
    if args.handoff_command == "validate":
        path = Path(args.path).expanduser().resolve()
        envelope = _read_bounded_json_contract(path, MAX_HANDOFF_BYTES, load_handoff)
        return {"ok": True, "action": "handoff-validate", "path": str(path), "handoff": envelope}
    raise AssertionError(f"Unhandled handoff command: {args.handoff_command}")


def _read_bounded_json_contract(path_value: str | Path, limit: int, loader: Any) -> dict[str, Any]:
    path = Path(path_value).expanduser().resolve()
    with path.open("rb") as handle:
        payload = handle.read(limit + 1)
    return loader(payload)


def _workflow_result(action: str, workflow: dict[str, Any], **extra: Any) -> dict[str, Any]:
    complete = is_workflow_complete(workflow)
    result: dict[str, Any] = {
        "ok": True,
        "action": action,
        "workflow": workflow,
        "complete": complete,
    }
    if complete:
        result["completion_token"] = workflow_completion_token(workflow)
    result.update(extra)
    return result


def _workflow(args: argparse.Namespace) -> dict[str, Any]:
    if args.workflow_command == "create":
        workflow = new_workflow({"goal_id": args.goal_id, "work_unit_id": args.work_unit_id})
        return _workflow_result("workflow-create", workflow)
    if args.workflow_command == "validate":
        path = Path(args.path).expanduser().resolve()
        workflow = _read_bounded_json_contract(path, MAX_WORKFLOW_BYTES, load_workflow)
        return _workflow_result("workflow-validate", workflow, path=str(path))
    if args.workflow_command == "accept":
        workflow = _read_bounded_json_contract(args.workflow_path, MAX_WORKFLOW_BYTES, load_workflow)
        handoff = _read_bounded_json_contract(args.handoff_path, MAX_HANDOFF_BYTES, load_handoff)
        accepted = accept_handoff(workflow, handoff)
        return _workflow_result("workflow-accept", accepted)
    raise AssertionError(f"Unhandled workflow command: {args.workflow_command}")


def _workspace(args: argparse.Namespace) -> dict[str, Any]:
    assessment = assess_workspace(_root(args.root))
    request = WorkspaceRequest(
        read_only=args.read_only,
        change_scope=args.change_scope,
        concurrent_workers=args.concurrent_workers,
        paths_known_disjoint=args.paths_known_disjoint,
        require_isolation=args.require_isolation,
    )
    recommendation = recommend_workspace(assessment, request)
    return {
        "ok": True,
        "action": "workspace-inspect",
        "assessment": {
            "requested_root": str(assessment.requested_root),
            "git_available": assessment.git_available,
            "is_repository": assessment.is_repository,
            "repository_root": str(assessment.repository_root) if assessment.repository_root else None,
            "branch": assessment.branch,
            "detached": assessment.detached,
            "head_revision": assessment.head_revision,
            "dirty": assessment.dirty,
            "has_untracked": assessment.has_untracked,
            "git_dir": str(assessment.git_dir) if assessment.git_dir else None,
            "common_git_dir": str(assessment.common_git_dir) if assessment.common_git_dir else None,
            "linked_worktree": assessment.linked_worktree,
            "worktree_identity": assessment.worktree_identity,
            "diagnostics": list(assessment.diagnostics),
            "local_work_can_continue": assessment.local_work_can_continue,
        },
        "recommendation": {"strategy": recommendation.strategy, "reasons": list(recommendation.reasons)},
    }


def _default_provider_registry() -> ProviderRegistry:
    """Return health-only optional providers without probing credentials/network."""
    registry = ProviderRegistry()
    registry.register_provider("github", discovery=GitHubCliAdapter(), operations={})
    registry.register_provider("jira", discovery=JiraConnectorAdapter(), operations={})
    return registry


def _host_provider_health(path_value: str) -> dict[str, Any]:
    """Load a credential-free, non-authoritative host capability snapshot."""
    path = Path(path_value)
    if not path.is_file():
        raise ProviderError(f"provider health snapshot is not a file: {path}")
    with path.open("rb") as stream:
        payload = stream.read(MAX_PROVIDER_JSON_BYTES + 1)
    if len(payload) > MAX_PROVIDER_JSON_BYTES:
        raise ProviderError(f"provider health snapshot exceeds the {MAX_PROVIDER_JSON_BYTES}-byte limit")
    report = load_bounded_provider_json(payload)
    validate_named(report, "provider-health-report")
    names: set[str] = set()
    for item in report["providers"]:
        name = item["provider"]
        if name in names:
            raise ProviderError(f"duplicate provider health entry: {name}")
        names.add(name)
        ProviderHealth.from_mapping({
            "provider": name,
            "state": item["state"],
            "summary": item["summary"],
        })
    return report


def _capabilities(args: argparse.Namespace, *, provider_registry: ProviderRegistry | None = None) -> dict[str, Any]:
    root = _root(args.root)
    config = load_project_config(root)
    git = assess_workspace(root)
    host_path = getattr(args, "provider_health", None)
    if provider_registry is not None:
        report = provider_registry.health_report()
        health_source = "embedded-host"
    elif host_path:
        report = _host_provider_health(host_path)
        health_source = "host-reported-snapshot"
    else:
        report = _default_provider_registry().health_report()
        health_source = "offline-default"
    capability_ids = {"github": "github-cli-adapter", "jira": "jira-connector-adapter"}
    provider_capabilities = [
        {
            "id": capability_ids.get(item["provider"], f"{item['provider']}-adapter"),
            "available": item["state"] in {"available", "degraded"},
            "optional": True,
            "configured": item["state"] != "unavailable",
            "state": item["state"],
            "status": item["summary"],
            "operations": item["operations"],
        }
        for item in report["providers"]
    ]
    return {
        "ok": True,
        "action": "capability-report",
        "local_work_can_continue": True,
        "provider_health_source": health_source,
        "provider_health_is_authority": False,
        "capabilities": [
            {"id": "local-work-items", "available": True, "optional": False},
            {"id": "structured-handoffs", "available": True, "optional": False},
            {"id": "validation", "available": True, "optional": False, "configured_commands": len(config.validation_commands)},
            {"id": "git", "available": git.git_available, "optional": True, "repository": git.is_repository},
            {"id": "research-adapter", "available": False, "optional": True, "configured": False,
             "state": "unavailable", "status": "No host research capability was supplied; local evidence remains usable."},
            {"id": "scheduler-host", "available": False, "optional": True, "configured": False,
             "state": "unavailable", "status": "No host scheduler capability was supplied; manual resume remains available."},
            *provider_capabilities,
        ],
        "provider_health": report,
    }


def _schedule(args: argparse.Namespace) -> dict[str, Any]:
    """Preview a schedule or safely resume one exact durable work unit."""
    if args.schedule_command == "resume":
        path = Path(args.plan).expanduser().resolve()
        if not path.is_file():
            raise SchedulerError(f"schedule resume plan is not a file: {path}")
        invocation = load_schedule_resume(path.read_bytes())
        if args.actor != invocation["lease"]["performer_id"]:
            raise SchedulerError("resume actor does not match the plan performer")
        token = os.environ.get(args.lease_token_env)
        if token is None:
            raise SchedulerError(f"lease token environment variable is not set: {args.lease_token_env}")
        root = _root(args.root)
        store = AutonomyStore(load_project_config(root).database_path(root))
        claim = reserve_schedule_resume(store, invocation, lease_token=token)
        return {
            "ok": True,
            "action": "schedule-resume",
            "mutation": "local-ledger",
            "idempotency_key": invocation["idempotency_key"],
            "recovered_attempts": claim["recovered_attempts"],
            "claim": claim,
        }

    health = None
    if args.scheduler_health:
        path = Path(args.scheduler_health).expanduser().resolve()
        if not path.is_file():
            raise SchedulerError(f"scheduler health snapshot is not a file: {path}")
        health = load_scheduler_health(path.read_bytes())
    return preview_schedule(
        _store(_root(args.root)), goal_id=args.goal_id, work_unit_id=args.work_unit_id,
        envelope_sha256=args.envelope_sha256, checkpoint_id=args.checkpoint,
        cadence=args.cadence, notification_intent=args.notification_intent,
        performer_id=args.performer_id, repository=args.repository,
        revision=args.revision, branch=args.branch, workspace=args.workspace,
        lease_seconds=args.lease_seconds, token_reservation=args.token_reservation,
        requested_adapter=args.adapter, health=health,
    )


def _release(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """Return a read-only release audit; this command cannot create approval evidence."""
    report = audit_release(_root(args.root), expected_version=args.expected_version)
    output = {"action": "release-audit", "mutation": "none", **report.as_dict()}
    return output, 0 if report.ok else 1


def _compile(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    root = _root(args.root)
    config = load_project_config(root)
    catalog_root = _catalog_root(root, args.catalog)
    catalog = load_catalog(
        catalog_root,
        source_trust=_catalog_source_trust(
            catalog_root,
            args.trust_catalog or config.catalog_trusted,
            allow_source_checkout=args.catalog is None,
        ),
    )
    activation = preflight_packs(
        catalog,
        config.enabled_packs or ("core",),
        available_capabilities=args.capability,
        trusted_executable_packs=args.trust_executable,
    )
    if not activation["ok"]:
        raise CatalogError(f"pack activation is blocked: {activation['blockers']}")
    policy, overrides = projection_overrides(catalog, config)
    projection = compile_catalog(
        catalog, config.enabled_packs or ("core",), codex_model_policy=policy,
        codex_role_overrides=overrides,
    )
    desired_manifest = build_generated_manifest(
        projection.files,
        tasktra_version=__version__,
        catalog_version=catalog.version,
        packs=projection.packs,
    )
    desired_lock = build_lockfile(
        desired_manifest,
        catalog_source_sha256=catalog_digest(catalog_root),
        pack_versions={identifier: catalog.packs[identifier].version for identifier in projection.packs},
        pack_contracts={
            identifier: {
                "version": catalog.packs[identifier].version,
                "contract_version": catalog.packs[identifier].contract_version,
                "trust": catalog.packs[identifier].trust,
                "sha256": catalog.packs[identifier].source_sha256,
            }
            for identifier in projection.packs
        },
        schema_versions={
            "configuration": config.version,
            "handoff": 1,
            "lesson-proposal": 1,
            "routing": 1,
            "runtime": STATE_SCHEMA_VERSION,
            "telemetry": 1,
            "work-item": 1,
            "workflow": 1,
        },
    )
    metadata_drift: list[str] = []
    try:
        previous_manifest = read_manifest(root)
    except FileNotFoundError:
        previous_manifest = None
        metadata_drift.append("generated manifest is missing")
    except ManifestError as error:
        previous_manifest = None
        metadata_drift.append(f"generated manifest is invalid: {error}")
    if previous_manifest is not None and previous_manifest != desired_manifest:
        metadata_drift.append("generated manifest does not match canonical projection")
    try:
        previous_lock = read_lockfile(root)
    except FileNotFoundError:
        previous_lock = None
        metadata_drift.append("lockfile is missing")
    except ManifestError as error:
        previous_lock = None
        metadata_drift.append(f"lockfile is invalid: {error}")
    if previous_lock is not None and previous_lock != desired_lock:
        metadata_drift.append("lockfile does not match canonical inputs and schemas")
    managed = {item.path for item in previous_manifest.files} if previous_manifest else set()
    drift = check_drift(root, projection, managed_paths=managed)
    locally_edited = _locally_edited_managed_outputs(root, drift.changed, previous_manifest)
    report = {
        "packs": list(projection.packs),
        "missing": [str(path) for path in drift.missing],
        "changed": [str(path) for path in drift.changed],
        "locally_edited": [str(path) for path in locally_edited],
        "stale": [str(path) for path in drift.stale],
        "project_owned": [str(path) for path in drift.project_owned],
        "metadata_drift": metadata_drift,
        "capability_preflight": activation,
        "source_hints": {
            path.as_posix(): _projection_source_hint(catalog, catalog_root, path)
            for path in sorted(set(drift.missing + drift.changed))
        },
    }
    if args.check:
        clean = drift.clean and not metadata_drift
        return {"ok": clean, "action": "compile-check", **report}, 0 if clean else 1

    unmanaged = sorted(
        relative.as_posix()
        for relative in projection.files
        if (root / Path(relative)).exists()
        and relative.as_posix() not in managed
        and not (previous_manifest is None and _resumable_generated_output(root / Path(relative), projection.files[relative]))
    )
    if unmanaged:
        raise ManifestError(
            "projection would replace project-owned files; preview and migrate them first: "
            + ", ".join(unmanaged)
        )
    if locally_edited and not args.force:
        raise ManifestError("managed projection has local edits; rerun with --force only after reviewing drift")
    if drift.stale and not args.prune_stale:
        raise ManifestError(
            "stale managed output remains; review it and rerun with --prune-stale "
            "to delete only files unchanged since the prior manifest"
        )
    if drift.stale:
        _prune_stale_managed_outputs(root, drift.stale, previous_manifest)

    written = write_projection(root, projection)
    write_manifest(root, desired_manifest)
    write_lockfile(root, desired_lock)
    return {
        "ok": True,
        "action": "compile",
        "written": [str(path.relative_to(root)) for path in written],
        "manifest_sha256": desired_manifest.digest,
        **report,
    }, 0


def _packs(args: argparse.Namespace) -> dict[str, Any]:
    root = _root(args.root)
    catalog_root = _catalog_root(root, args.catalog)
    config = load_project_config(root)
    catalog = load_catalog(
        catalog_root,
        source_trust=_catalog_source_trust(
            catalog_root,
            args.trust_catalog or config.catalog_trusted,
            allow_source_checkout=args.catalog is None,
        ),
    )
    selected = tuple(args.pack) if getattr(args, "pack", None) else config.enabled_packs
    if args.pack_command == "recommend":
        return {
            "ok": True,
            "action": "pack-recommendation-preview",
            **recommend_packs(root, catalog, max_files=args.max_files, max_depth=args.max_depth).to_dict(),
        }
    if args.pack_command == "preflight":
        return {
            "action": "pack-preflight",
            **preflight_packs(
                catalog,
                selected,
                available_capabilities=args.capability,
                trusted_executable_packs=args.trust_executable,
            ),
        }
    if args.pack_command == "migration-preview":
        try:
            locked = dict(read_lockfile(root).pack_versions)
        except FileNotFoundError:
            locked = {}
        return {
            "action": "pack-migration-preview",
            **preview_pack_migrations(
                catalog,
                locked,
                selected,
                trusted_executable_packs=args.trust_executable,
            ),
        }
    raise AssertionError(f"Unhandled pack command: {args.pack_command}")


def _jira_sync(args: argparse.Namespace) -> dict[str, Any]:
    """Return a closed Jira transition plan; this command never contacts Jira."""
    root = _root(args.root)
    config = load_project_config(root)
    if "jira-sync" not in config.enabled_packs or config.jira_sync is None:
        raise JiraSyncError(
            "Jira synchronization is not enabled; add the optional jira-sync pack and [jira_sync] policy first"
        )
    if args.jira_sync_command == "plan":
        return {
            "ok": True,
            "action": "jira-sync-plan",
            "local_work_can_continue": True,
            **build_sync_plan(
                policy=config.jira_sync, event=args.event, issue=args.issue,
                goal_id=args.goal_id, work_unit_id=args.work_unit_id,
            ),
        }
    raise AssertionError(f"Unhandled Jira sync command: {args.jira_sync_command}")


def _delegation(args: argparse.Namespace) -> dict[str, Any]:
    root = _root(args.root)
    config = load_project_config(root)
    catalog_root = _catalog_root(root, None)
    catalog = load_catalog(
        catalog_root,
        source_trust=_catalog_source_trust(
            catalog_root, config.catalog_trusted, allow_source_checkout=True
        ),
    )
    request = _runtime_json(args.request)
    handoff = _read_bounded_json_contract(args.handoff, MAX_HANDOFF_BYTES, load_handoff) if args.handoff else None
    return {"ok": True, "action": "delegation-plan", **delegation_plan(catalog, config, request, handoff=handoff)}


def _lifecycle_inputs(args: argparse.Namespace) -> tuple[Path, Any, Any, Path]:
    root = _root(args.root)
    config = load_project_config(root)
    catalog_root = _catalog_root(root, getattr(args, "catalog", None))
    catalog = load_catalog(
        catalog_root,
        source_trust=_catalog_source_trust(
            catalog_root,
            bool(getattr(args, "trust_catalog", False) or config.catalog_trusted),
            allow_source_checkout=getattr(args, "catalog", None) is None,
        ),
    )
    return root, config, catalog, catalog_root


def _adopt(args: argparse.Namespace) -> dict[str, Any]:
    root = _root(args.root)
    catalog_root = _catalog_root(root, args.catalog)
    trusted = bool(args.trust_catalog)
    try:
        config = load_project_config(root)
    except FileNotFoundError:
        config = None
    if config is not None:
        trusted = trusted or config.catalog_trusted
    catalog = load_catalog(
        catalog_root,
        source_trust=_catalog_source_trust(
            catalog_root, trusted, allow_source_checkout=args.catalog is None
        ),
    )
    selected = tuple(args.pack) or ((config.enabled_packs if config is not None else ()) or ("core",))
    validations = config.validation_commands if config is not None else ()
    policy, overrides = projection_overrides(catalog, config) if config is not None else (None, None)
    return preview_adoption(
        root,
        catalog,
        enabled_packs=selected,
        available_capabilities=args.capability,
        trusted_executable_packs=args.trust_executable,
        validation_commands=validations,
        codex_model_policy=policy,
        codex_role_overrides=overrides,
    ).as_dict()


def _upgrade_preview(args: argparse.Namespace) -> tuple[Path, Any, Any, Path, dict[str, Any]]:
    root, config, catalog, catalog_root = _lifecycle_inputs(args)
    selected = tuple(args.pack) if args.pack else None
    policy, overrides = projection_overrides(catalog, config)
    plan = preview_upgrade(
        root,
        catalog,
        enabled_packs=selected,
        available_capabilities=args.capability,
        trusted_executable_packs=args.trust_executable,
        codex_model_policy=policy,
        codex_role_overrides=overrides,
    ).as_dict()
    return root, config, catalog, catalog_root, plan


def _upgrade(args: argparse.Namespace) -> dict[str, Any]:
    root = _root(args.root)
    if args.upgrade_command == "rollback":
        if not args.confirm:
            raise UpgradeError("upgrade rollback requires --confirm")
        store = _autonomy(root)
        request = {
            "action": "upgrade-rollback",
            "snapshot_plan_sha256": args.snapshot_plan_sha256,
            "before_sha256": args.before_sha256,
        }
        intent = store.prepare_effect(
            idempotency_key=args.idempotency_key,
            goal_id=args.goal_id,
            work_unit_id=args.work_unit_id,
            effect_class="local-reversible-write",
            operation="local-effect",
            request=request,
            envelope_sha256=args.envelope_sha256,
            performer_id=args.actor,
        )
        if intent.get("status") != "pending":
            raise UpgradeError(f"rollback effect is not dispatchable: {intent.get('status')}")
        try:
            result = rollback_upgrade(
                root,
                args.snapshot_plan_sha256,
                expected_before_sha256=args.before_sha256,
            )
        except Exception as error:
            store.record_effect_receipt(
                idempotency_key=args.idempotency_key,
                outcome="failed",
                evidence={"action": "upgrade-rollback", "error": str(error), "restored": False},
                performer_id=args.actor,
            )
            raise
        receipt = store.record_effect_receipt(
            idempotency_key=args.idempotency_key,
            outcome="success",
            evidence=result,
            performer_id=args.actor,
            before_sha256=args.before_sha256,
            after_sha256=str(result["restored_sha256"]),
        )
        return {**result, "effect_receipt": receipt}

    root, config, catalog, catalog_root, plan = _upgrade_preview(args)
    plan_sha256 = upgrade_plan_digest(plan)
    if args.upgrade_command == "preview":
        return {**plan, "plan_sha256": plan_sha256}
    if not args.confirm:
        raise UpgradeError("upgrade apply requires --confirm")
    if args.plan_sha256 != plan_sha256:
        raise UpgradeError("upgrade plan digest changed; preview again before applying")
    store = _autonomy(root)
    request = {"action": "upgrade-apply", "plan_sha256": plan_sha256}
    intent = store.prepare_effect(
        idempotency_key=args.idempotency_key,
        goal_id=args.goal_id,
        work_unit_id=args.work_unit_id,
        effect_class="local-reversible-write",
        operation="local-effect",
        request=request,
        envelope_sha256=args.envelope_sha256,
        performer_id=args.actor,
    )
    if intent.get("status") != "pending":
        raise UpgradeError(f"upgrade effect is not dispatchable: {intent.get('status')}")
    try:
        result = apply_upgrade(
            root,
            catalog_root,
            catalog,
            config,
            plan,
            expected_plan_sha256=plan_sha256,
            confirmed=True,
            allow_network=args.allow_network,
            timeout_seconds=args.timeout,
        )
    except Exception as error:
        store.record_effect_receipt(
            idempotency_key=args.idempotency_key,
            outcome="failed",
            evidence={"action": "upgrade-apply", "plan_sha256": plan_sha256, "error": str(error)},
            performer_id=args.actor,
        )
        raise
    migration = result["migration"]
    receipt = store.record_effect_receipt(
        idempotency_key=args.idempotency_key,
        outcome="success",
        evidence={"action": "upgrade-apply", "plan_sha256": plan_sha256, "migration": migration},
        performer_id=args.actor,
        before_sha256=str(migration["before_sha256"]),
        after_sha256=str(migration["after_sha256"]),
    )
    return {**result, "effect_receipt": receipt}


def _telemetry(args: argparse.Namespace) -> dict[str, Any]:
    root = _root(args.root)
    if args.telemetry_command == "status":
        records = TelemetryStore(root, enabled=False).records()
        return {
            "ok": True,
            "action": "telemetry-status",
            "collection_enabled": False,
            "local_only": True,
            "record_count": len(records),
        }
    if args.telemetry_command == "record":
        if not args.enable:
            raise TelemetryError("telemetry append is disabled by default; pass --enable for this local record")
        value = _runtime_json(args.path)
        store = TelemetryStore(root, enabled=True)
        appended = store.append(value)
        return {"ok": True, "action": "telemetry-record", "appended": appended, "local_only": True}
    store = TelemetryStore(root, enabled=False)
    destination = store.export_sanitized(args.destination)
    return {"ok": True, "action": "telemetry-export", "path": str(destination), "sanitized": True}


def _bounded_json_value(path_value: str, *, limit: int = _MAX_RUNTIME_JSON_BYTES) -> Any:
    path = Path(path_value).expanduser().resolve()
    with path.open("rb") as handle:
        payload = handle.read(limit + 1)
    if len(payload) > limit:
        raise ValueError(f"JSON input exceeds {limit} bytes")
    return json.loads(payload.decode("utf-8"))


def _benchmark(args: argparse.Namespace) -> dict[str, Any]:
    baseline = _bounded_json_value(args.baseline)
    candidate = _bounded_json_value(args.candidate)
    if not isinstance(baseline, list) or not isinstance(candidate, list):
        raise ValueError("benchmark inputs must be JSON arrays")
    plan = BenchmarkPlan(tuple(args.scenario)) if args.scenario else None
    report = compare_observations(baseline, candidate, plan=plan)
    return {
        "ok": not report.has_regressions,
        "action": "benchmark-compare",
        "compared_scenarios": list(report.compared_scenarios),
        "findings": [
            {"kind": item.kind, "scenario": item.scenario, "detail": item.detail}
            for item in report.findings
        ],
        "measurements": [dict(item) for item in report.measurements],
        "estimated_token_savings": None,
    }


def _lesson(args: argparse.Namespace) -> dict[str, Any]:
    store = LessonProposalStore(_root(args.root))
    if args.lesson_command == "list":
        return {"ok": True, "action": "lesson-list", "proposals": [item.to_dict() for item in store.list()]}
    if args.lesson_command == "show":
        return {"ok": True, "action": "lesson-show", "proposal": store.read(args.proposal_id).to_dict()}
    if args.lesson_command == "promotion-preview":
        return {"ok": True, "action": "lesson-promotion-preview", "plan": store.promotion_plan(args.proposal_id)}
    if args.lesson_command == "create":
        value = _runtime_json(args.path)
        required = {
            "proposal_id", "author", "problem", "general_principle", "evidence",
            "affected_contracts", "applicability", "risks", "regression_checks",
        }
        if set(value) != required:
            raise LessonError("lesson create input has missing or unknown fields")
        proposal = store.create(**value)
        return {"ok": True, "action": "lesson-create", "proposal": proposal.to_dict()}
    status = {"review": "reviewed", "approve": "approved", "reject": "rejected"}[args.lesson_command]
    proposal = store.transition(
        args.proposal_id,
        expected_version=args.expected_version,
        to_status=status,
        actor=args.actor,
        reason=getattr(args, "reason", None),
    )
    return {"ok": True, "action": f"lesson-{args.lesson_command}", "proposal": proposal.to_dict()}


def _prune_stale_managed_outputs(
    root: Path,
    stale: tuple[Any, ...],
    previous_manifest: Any,
) -> None:
    """Delete only stale files whose bytes still match their ownership record."""
    if previous_manifest is None:
        raise ManifestError("cannot prune stale output without a prior generated manifest")
    records = {item.path: item for item in previous_manifest.files}
    project = root.resolve()
    candidates: list[Path] = []
    for relative in stale:
        record = records.get(relative.as_posix())
        if record is None:
            raise ManifestError(f"stale path is not owned by the prior manifest: {relative}")
        path = project.joinpath(*relative.parts)
        cursor = project
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ManifestError(f"refusing to prune a path that crosses a symlink: {relative}")
        try:
            path.resolve(strict=True).relative_to(project)
        except (FileNotFoundError, ValueError) as error:
            raise ManifestError(f"stale managed output is unsafe or missing: {relative}") from error
        if not path.is_file():
            raise ManifestError(f"stale managed output is not a regular file: {relative}")
        if sha256_bytes(path.read_bytes()) != record.sha256:
            raise ManifestError(f"stale managed output has local edits and will not be pruned: {relative}")
        candidates.append(path)
    for path in candidates:
        path.unlink()


def _locally_edited_managed_outputs(
    root: Path,
    changed: tuple[Any, ...],
    previous_manifest: Any,
) -> tuple[Any, ...]:
    """Separate source-driven regeneration from edits to generated copies."""
    if previous_manifest is None:
        return changed
    records = {item.path: item for item in previous_manifest.files}
    edited: list[Any] = []
    for relative in changed:
        record = records.get(relative.as_posix())
        path = root.joinpath(*relative.parts)
        if record is None or sha256_bytes(path.read_bytes()) != record.sha256:
            edited.append(relative)
    return tuple(edited)


def _catalog_root(root: Path, requested: str | None) -> Path:
    if requested:
        return _root(requested)
    project_catalog = root / "catalog"
    if project_catalog.is_dir():
        return project_catalog
    packaged_catalog = Path(__file__).resolve().with_name("catalog")
    if packaged_catalog.is_dir():
        return packaged_catalog
    # An editable source install does not run ``build_py``, so its canonical
    # catalog remains at the repository root instead of beside the package.
    # Treat it as built-in because it shares the same source boundary as the
    # imported Tasktra code.
    source_catalog = Path(__file__).resolve().parents[2] / "catalog"
    if (source_catalog / "catalog.toml").is_file():
        return source_catalog
    installed_catalog = Path(sysconfig.get_path("data")) / "share" / "tasktra" / "catalog"
    if installed_catalog.is_dir():
        return installed_catalog
    raise FileNotFoundError(
        "Tasktra catalog is unavailable; install the package with data files or pass --catalog"
    )


def _catalog_source_trust(
    catalog_root: Path, explicitly_trusted: bool, *, allow_source_checkout: bool = False
) -> str:
    if explicitly_trusted:
        return "builtin"
    packaged = Path(__file__).resolve().with_name("catalog")
    source = Path(__file__).resolve().parents[2] / "catalog"
    installed = Path(sysconfig.get_path("data")) / "share" / "tasktra" / "catalog"
    try:
        if (
            (packaged.is_dir() and catalog_root.resolve() == packaged.resolve())
            or (
                allow_source_checkout
                and (source / "catalog.toml").is_file()
                and catalog_root.resolve() == source.resolve()
            )
            or (installed.is_dir() and catalog_root.resolve() == installed.resolve())
        ):
            return "builtin"
    except OSError:
        pass
    return "third-party-data-only"


def _projection_source_hint(catalog: Any, catalog_root: Path, path: Any) -> str:
    parts = path.parts
    if len(parts) >= 3 and parts[1] in {"agents", "roles"}:
        identifier = Path(parts[-1]).stem
        document = catalog.roles.get(identifier)
        return str(document.source) if document else str(catalog_root / "catalog.toml")
    if "skills" in parts and len(parts) >= 3:
        identifier = parts[parts.index("skills") + 1]
        document = catalog.skills.get(identifier)
        return str(document.source) if document else str(catalog_root / "catalog.toml")
    return str(Path(__file__).resolve().with_name("compiler.py"))


def _resumable_generated_output(path: Path, expected: str) -> bool:
    """Recognize a completed atomic file from an interrupted first compile."""
    try:
        actual = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return actual == expected and "Generated by Tasktra. Edit catalog sources" in actual


def _instruction_summary(preview: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in preview.instructions:
        root = item.path.split("/", 1)[0] if "/" in item.path else "top-level"
        counts[root] = counts.get(root, 0) + 1
    return dict(sorted(counts.items()))


def _goal(args: argparse.Namespace) -> dict[str, Any]:
    store = _store(_root(args.root))
    if args.goal_command == "create":
        result = store.create_goal(title=args.title, description=args.description, goal_id=args.goal_id,
            priority=args.priority, acceptance=args.acceptance, budget_tokens=args.budget)
        return {"ok": True, "goal": result, "budget": store.budget_summary(result["id"])}
    if args.goal_command == "list":
        return {"ok": True, "goals": store.list_goals(status=args.status)}
    if args.goal_command == "show":
        result = store.get_goal(args.goal_id)
        if result is None:
            raise StateError(f"Unknown goal: {args.goal_id}")
        return {"ok": True, "goal": result, "budget": store.budget_summary(args.goal_id), "checkpoints": store.get_goal_checkpoints(args.goal_id)}
    if args.goal_command in {"activate", "resume", "complete"}:
        runtime = _autonomy(_root(args.root))
        if args.goal_command == "activate": result = runtime.activate_goal(args.goal_id, actor_id=args.actor, envelope_sha256=args.envelope_sha256)
        elif args.goal_command == "resume": result = runtime.resume_goal(args.goal_id, actor_id=args.actor, envelope_sha256=args.envelope_sha256)
        else: result = runtime.complete_goal(goal_id=args.goal_id, performer_id=args.actor, envelope_sha256=args.envelope_sha256)
        return {"ok": True, "goal": result}
    if args.goal_command in {"pause", "stop"}:
        result = getattr(store, f"{args.goal_command}_goal")(args.goal_id, actor_id=args.actor)
        return {"ok": True, "goal": result}
    raise AssertionError(f"Unhandled goal command: {args.goal_command}")


def _runtime_json(path: str | Path, loader: Any = None) -> dict[str, Any]:
    def reject_duplicates(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result: raise StateError(f"duplicate JSON key: {key}")
            result[key] = value
        return result
    def reject_constant(value: str) -> None:
        raise StateError(f"non-finite JSON value: {value}")
    path = Path(path).expanduser().resolve()
    with path.open("rb") as handle: payload = handle.read(_MAX_RUNTIME_JSON_BYTES + 1)
    if len(payload) > _MAX_RUNTIME_JSON_BYTES: raise StateError("runtime JSON exceeds 64KiB")
    value = json.loads(payload, object_pairs_hook=reject_duplicates, parse_constant=reject_constant)
    if not isinstance(value, dict): raise StateError("runtime JSON must be an object")
    return loader(json.dumps(value, separators=(",", ":"), sort_keys=True).encode()) if loader else value


def _lease_token(name: str) -> str:
    if not name or not name.replace("_", "a").isalnum():
        raise StateError("lease token environment variable name is invalid")
    token = os.environ.get(name)
    if not token:
        raise StateError(f"lease token environment variable is unset: {name}")
    return token


def _state(args: argparse.Namespace) -> dict[str, Any]:
    root = _root(args.root); config = load_project_config(root); path = config.database_path(root)
    if args.state_command == "attest-ledger":
        return {"ok": True, "action": "state-attest-ledger", "attestation": StateStore(path).attest_ledger(actor_id=args.human_actor)}
    if args.state_command == "migrate" and args.preview:
        return {"ok": True, "action": "state-migrate-preview", "database": str(path), "exists": path.exists(), "current_schema": StateStore(path).inspect_schema_version() if path.exists() else 0, "target_schema": STATE_SCHEMA_VERSION}
    if path.exists():
        current = StateStore(path).inspect_schema_version()
        if current not in {0, STATE_SCHEMA_VERSION}:
            raise StateError(
                "existing runtime schema migration requires an exact authority-bound "
                "tasktra upgrade preview/apply plan"
            )
    return {"ok": True, "action": "state-migrate-apply", "schema_version": StateStore(path).migrate()}


def _runtime(args: argparse.Namespace) -> dict[str, Any]:
    store = _store(_root(args.root))
    changed = store.set_emergency_stop(actor_id=args.actor, reason=args.reason) if args.runtime_command == "emergency-stop" else store.clear_emergency_stop(actor_id=args.actor, approver_kind=args.actor_kind)
    return {"ok": True, "action": f"runtime-{args.runtime_command}", "changed": changed}


def _contract(args: argparse.Namespace) -> dict[str, Any]:
    value = _runtime_json(args.path, load_authority_envelope)
    return {"ok": True, "contract": _store(_root(args.root)).define_goal_contract(args.goal_id, value, actor_id=args.actor)}


def _approval(args: argparse.Namespace) -> dict[str, Any]:
    store = _store(_root(args.root))
    if args.approval_command == "revoke": return {"ok": True, "approval": store.revoke_transition_approval(args.approval_id, actor_id=args.actor)}
    value = _runtime_json(args.path, load_transition_approval)
    if args.approval_command == "repair-scope":
        return {"ok": True, "action": "approval-repair-scope", "approval": store.repair_unsealed_transition_approval(args.approval_id, value, actor_id=args.human_actor, actor_kind="human")}
    provenance = value.get("provenance")
    if (
        value["decision"] == "approved"
        and value["approver"]["kind"] == "human"
        and value["version"] < 3
    ):
        raise StateError(
            "approved human approval import requires v3 local-terminal or v4 Codex-message provenance"
        )
    if value["version"] == 3:
        if args.human_actor != value["approver"]["id"]:
            raise StateError("v3 approval import requires --human-actor matching approver.id")
        if not sys.stdin.isatty():
            raise StateError("v3 approval import requires an interactive local human ceremony; use a local terminal")
        subject_sha256 = transition_approval_subject_sha256(value)
        confirmation = input(
            "Local human ceremony (not identity authentication). Type the approval subject hash to record: "
        )
        if confirmation.strip() != subject_sha256:
            raise StateError("local human ceremony confirmation did not match the approval subject hash")
        # Ceremony metadata is an observation made by this local CLI, not a
        # claim trusted from the imported document. The subject hash excludes
        # provenance, so replacing these fields preserves the exact approval
        # the human just confirmed while preventing caller-supplied timestamps.
        provenance = {
            "kind": "local-human-ceremony",
            "attester_id": args.human_actor,
            "subject_sha256": subject_sha256,
            "attested_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }
    elif value["version"] == 4:
        if args.human_actor != value["approver"]["id"]:
            raise StateError("v4 Codex approval import requires --human-actor matching approver.id")
        message = args.codex_user_message
        if not isinstance(message, str) or not message.strip():
            raise StateError("v4 Codex approval import requires an explicit --codex-user-message")
        normalized_message = message.casefold()
        if len(message) > 2_000 or not any(term in normalized_message for term in ("approve", "authorize")):
            raise StateError("Codex approval message must be a short explicit approval or authorization")
        subject_sha256 = transition_approval_subject_sha256(value)
        provenance = {
            "kind": "codex-user-message",
            "attester_id": args.human_actor,
            "subject_sha256": subject_sha256,
            "attested_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "message_sha256": sha256(message.strip().encode("utf-8")).hexdigest(),
        }
    approval = store.record_transition_approval(goal_id=value["goal_id"], work_unit_id=value["work_unit_id"], action=value["action"], effect=value["effect"], scope=value["scope"], envelope_sha256=value["envelope_sha256"], decision=value["decision"], approver_id=value["approver"]["id"], approver_kind=value["approver"]["kind"], performer_id=value["performer_id"], authority_clause=value["authority_clause"], evidence=value["evidence"], valid_until=value["valid_until"], approval_id=value["approval_id"], resource_scope=value.get("resource_scope"), provenance=provenance)
    return {"ok": True, "approval": approval}


def _work(args: argparse.Namespace) -> dict[str, Any]:
    root = _root(args.root); store = _autonomy(root)
    if args.work_command == "create":
        scope = _runtime_json(args.scope) if args.scope else {}
        return {"ok": True, "work_unit": store.create_work_unit(goal_id=args.goal_id, title=args.title, work_unit_id=args.id, scope=scope, checkpoint_id=args.checkpoint)}
    if args.work_command == "assign-checkpoint":
        return {"ok": True, "work_unit": store.assign_work_unit_checkpoint(args.work_unit_id, args.checkpoint_id, actor_id=args.human_actor, actor_kind="human")}
    if args.work_command == "claim":
        result = store.claim_next_work(goal_id=args.goal_id, performer_id=args.actor, envelope_sha256=args.envelope_sha256, lease_seconds=args.lease_seconds, token_reservation=args.token_reservation, repository=args.repository, revision=args.revision, branch=args.branch, workspace=args.workspace, lease_token=_lease_token(args.lease_token_env))
        return {"ok": True, "claim": result}
    if args.work_command == "heartbeat": return {"ok": True, "heartbeat": store.heartbeat(attempt_id=args.attempt_id, performer_id=args.actor, lease_token=_lease_token(args.lease_token_env), lease_seconds=args.lease_seconds)}
    if args.work_command == "finish":
        workflow = _runtime_json(args.workflow, load_workflow) if args.workflow else None
        evidence = _runtime_json(args.evidence_json) if args.evidence_json else {}
        return {"ok": True, "finish": store.finish_attempt(attempt_id=args.attempt_id, performer_id=args.actor, lease_token=_lease_token(args.lease_token_env), outcome=args.outcome, tokens_consumed=args.tokens_consumed, workflow=workflow, outcome_evidence=evidence)}
    if args.work_command == "requeue":
        return {"ok": True, "work_unit": store.requeue_work(work_unit_id=args.work_unit_id, performer_id=args.actor, envelope_sha256=args.envelope_sha256, evidence=_runtime_json(args.evidence_json))}
    return {"ok": True, "recovered": store.recover_expired_leases(goal_id=args.goal_id)}


def _acceptance_evidence(args: argparse.Namespace) -> dict[str, Any]:
    item = _autonomy(_root(args.root)).record_acceptance_evidence(goal_id=args.goal_id, criterion_id=args.criterion_id, evidence=_runtime_json(args.evidence), performer_id=args.actor, envelope_sha256=args.envelope_sha256)
    return {"ok": True, "evidence": item}


def _effect(args: argparse.Namespace) -> dict[str, Any]:
    store = _autonomy(_root(args.root))
    if args.effect_command == "prepare":
        item = store.prepare_effect(idempotency_key=args.key, goal_id=args.goal_id, work_unit_id=args.work_unit_id, effect_class=args.effect_class, operation=args.operation, request=_runtime_json(args.request), envelope_sha256=args.envelope_sha256, performer_id=args.actor)
    elif args.effect_command == "receipt":
        item = store.record_effect_receipt(idempotency_key=args.key, outcome=args.outcome, evidence=_runtime_json(args.evidence), performer_id=args.actor, before_sha256=args.before_sha256, after_sha256=args.after_sha256)
    elif args.effect_command == "provider-prepare":
        descriptor = OperationDescriptor.from_mapping(_runtime_json(args.descriptor))
        item = store.prepare_provider_effect(idempotency_key=args.key, goal_id=args.goal_id, work_unit_id=args.work_unit_id, operation_descriptor=descriptor, request=_runtime_json(args.request), envelope_sha256=args.envelope_sha256, performer_id=args.actor, work_attempt_id=args.work_attempt_id, lease_token=_lease_token(args.lease_token_env))
    elif args.effect_command == "provider-execute":
        descriptor = OperationDescriptor.from_mapping(_runtime_json(args.descriptor))
        if descriptor.provider != "github":
            raise ProviderError("no configured executor-backed adapter is available for this provider")
        adapter = GitHubCliAdapter(BoundedArgvRunner())
        registry = ProviderRegistry()
        registry.register_provider("github", discovery=adapter, operations={descriptor: adapter})
        item = ProviderEffectExecutor(store, registry).execute(
            idempotency_key=args.key, operation_descriptor=descriptor, performer_id=args.actor,
            lease_token=_lease_token(args.lease_token_env),
        )
    elif args.effect_command == "provider-reconcile":
        item = store.reconcile_provider_effect(idempotency_key=args.key, resolution=args.resolution, observation=_runtime_json(args.observation), performer_id=args.actor)
    else:
        item = store.inspect_effect(args.key)
    return {"ok": True, "effect": item}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            output, code = _init(args), 0
        elif args.command == "bootstrap":
            output, code = _bootstrap(args), 0
        elif args.command == "status":
            output, code = _status(args), 0
        elif args.command == "doctor":
            output, code = _doctor(args)
        elif args.command == "validate":
            output, code = _validate_project(args)
        elif args.command == "work-item":
            output, code = _work_item(args), 0
        elif args.command == "handoff":
            output, code = _handoff(args), 0
        elif args.command == "workflow":
            output, code = _workflow(args), 0
        elif args.command == "workspace":
            output, code = _workspace(args), 0
        elif args.command == "capabilities":
            output, code = _capabilities(args), 0
        elif args.command == "schedule":
            output, code = _schedule(args), 0
        elif args.command == "release":
            output, code = _release(args)
        elif args.command == "compile":
            output, code = _compile(args)
        elif args.command == "delegation":
            output, code = _delegation(args), 0
        elif args.command == "packs":
            output, code = _packs(args), 0
        elif args.command == "jira-sync":
            output, code = _jira_sync(args), 0
        elif args.command == "adopt":
            output, code = _adopt(args), 0
        elif args.command == "upgrade":
            output, code = _upgrade(args), 0
        elif args.command == "telemetry":
            output, code = _telemetry(args), 0
        elif args.command == "benchmark":
            output = _benchmark(args)
            code = 0 if output["ok"] else 1
        elif args.command == "lesson":
            output, code = _lesson(args), 0
        elif args.command == "goal":
            output, code = _goal(args), 0
        elif args.command == "state":
            output, code = _state(args), 0
        elif args.command == "runtime":
            output, code = _runtime(args), 0
        elif args.command == "contract":
            output, code = _contract(args), 0
        elif args.command == "approval":
            output, code = _approval(args), 0
        elif args.command == "work":
            output, code = _work(args), 0
        elif args.command == "audit":
            output, code = _audit(args), 0
        elif args.command == "acceptance-evidence":
            output, code = _acceptance_evidence(args), 0
        elif args.command == "effect":
            output, code = _effect(args), 0
        else:
            raise AssertionError(f"Unhandled command: {args.command}")
    except (CatalogError, ConfigError, FileExistsError, FileNotFoundError, HandoffError, LessonError, LifecycleError, ManifestError, MigrationError, SchedulerError, StateError, TelemetryError, UpgradeError, ValidationError, WorkflowError, WorkItemError, OSError, ValueError) as error:
        _emit({"ok": False, "error": str(error)}, stream=sys.stderr)
        return 2
    _emit(output)
    return code
