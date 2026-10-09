"""Fixtures for making a current temporary runtime structurally historical."""

from __future__ import annotations

import sqlite3


_INTERVENTION_TABLES = (
    "intervention_closures",
    "intervention_response_heads",
    "intervention_responses",
    "intervention_requests",
)
_INTERVENTION_INDEXES = (
    "intervention_requests_goal_created",
    "intervention_requests_unit_created",
    "work_units_current_intervention",
    "intervention_responses_request_created",
    "intervention_response_heads_response",
    "intervention_closures_closed_at",
    "intervention_closures_closed_request",
    "intervention_responses_predecessor_once",
    "work_unit_dependencies_prerequisite",
)
_INTERVENTION_TRIGGERS = (
    "intervention_requests_no_update",
    "intervention_requests_no_delete",
    "intervention_responses_no_update",
    "intervention_responses_no_delete",
    "intervention_closures_no_update",
    "intervention_closures_no_delete",
    "intervention_response_heads_no_delete",
)

_CODEX_RUN_TABLES = (
    "codex_run_finishes",
    "codex_run_starts",
    "codex_run_preparations",
)
_CODEX_RUN_INDEXES = (
    "codex_run_preparations_attempt_run",
    "codex_run_preparations_goal_created",
    "codex_run_starts_agent",
    "codex_run_finishes_recorded",
)
_CODEX_RUN_TRIGGERS = (
    "codex_run_preparations_no_update",
    "codex_run_preparations_no_delete",
    "codex_run_starts_no_update",
    "codex_run_starts_no_delete",
    "codex_run_finishes_no_update",
    "codex_run_finishes_no_delete",
)

_DEPENDENCY_INDEXES = (
    "work_unit_dependencies_prerequisite",
)


def peel_schema11_dependencies(connection: sqlite3.Connection, *, target_version: int) -> None:
    """Remove every schema-11 dependency object for older fixture shapes.

    The v13 intervention migration also creates an index on this table, so an
    older synthetic fixture must drop that index before it removes the table.
    Leaving either object behind makes a database that advertises an earlier
    schema structurally impossible and masks the fail-closed migration guard.
    """
    if target_version >= 15:
        raise ValueError("target_version must predate schema 15")
    if target_version >= 11:
        return
    for index in _DEPENDENCY_INDEXES:
        connection.execute(f"DROP INDEX IF EXISTS {index}")
    connection.execute("DROP TABLE IF EXISTS work_unit_dependencies")


def peel_schema15_policy(connection: sqlite3.Connection, *, target_version: int) -> None:
    """Remove the remote v11/v12 lineage before advertising a pre-v15 shape."""
    if target_version >= 15:
        raise ValueError("target_version must predate schema 15")
    work_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(work_units)")}
    if "acceptance_checks" in work_columns:
        connection.execute("ALTER TABLE work_units DROP COLUMN acceptance_checks")
    if "verification_policy" in work_columns:
        connection.execute("ALTER TABLE work_units DROP COLUMN verification_policy")
    evidence_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(workflow_evidence)")}
    if "completion_evidence_json" in evidence_columns:
        connection.execute("ALTER TABLE workflow_evidence DROP COLUMN completion_evidence_json")


def peel_schema14_codex_runs(connection: sqlite3.Connection, *, target_version: int) -> None:
    """Remove every v14-only object before a fixture advertises an older schema.

    Production migration deliberately rejects any of these objects below v14.
    Synthetic fixtures must remove them, including the accounting provenance
    column, before they lower ``user_version``.
    """
    if target_version >= 15:
        raise ValueError("target_version must predate schema 15")
    peel_schema15_policy(connection, target_version=target_version)
    if target_version >= 14:
        return
    for trigger in _CODEX_RUN_TRIGGERS:
        connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    for index in _CODEX_RUN_INDEXES:
        connection.execute(f"DROP INDEX IF EXISTS {index}")
    for table in _CODEX_RUN_TABLES:
        connection.execute(f"DROP TABLE IF EXISTS {table}")
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(work_attempts)")}
    if "token_accounting_source" in columns:
        connection.execute("ALTER TABLE work_attempts DROP COLUMN token_accounting_source")

    names = (*_CODEX_RUN_TABLES, *_CODEX_RUN_INDEXES, *_CODEX_RUN_TRIGGERS)
    placeholders = ",".join("?" for _ in names)
    present = connection.execute(
        f"SELECT name FROM sqlite_master WHERE name IN ({placeholders}) ORDER BY name", names,
    ).fetchall()
    if present:
        raise AssertionError(f"schema-14 fixture objects remain: {[row[0] for row in present]}")
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(work_attempts)")}
    if "token_accounting_source" in columns:
        raise AssertionError("schema-14 token accounting source remains")


def peel_schema13_interventions(connection: sqlite3.Connection, *, target_version: int) -> None:
    """Peel current objects before a fixture advertises a pre-v13 schema.

    A schema-13 fixture only needs v14 peeled.  Older fixtures also lose v13.
    Production migration remains deliberately strict about either shape; this
    helper makes synthetic fixture versions structurally honest instead of
    masking a partially upgraded ledger.
    """
    if target_version >= 15:
        raise ValueError("target_version must predate schema 15")
    peel_schema14_codex_runs(connection, target_version=target_version)
    peel_schema11_dependencies(connection, target_version=target_version)
    if target_version >= 13:
        return
    for trigger in _INTERVENTION_TRIGGERS:
        connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    for index in _INTERVENTION_INDEXES:
        connection.execute(f"DROP INDEX IF EXISTS {index}")
    for table in _INTERVENTION_TABLES:
        connection.execute(f"DROP TABLE IF EXISTS {table}")
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(work_units)")}
    if "current_intervention_id" in columns:
        connection.execute("ALTER TABLE work_units DROP COLUMN current_intervention_id")

    names = (*_INTERVENTION_TABLES, *_INTERVENTION_INDEXES, *_INTERVENTION_TRIGGERS)
    placeholders = ",".join("?" for _ in names)
    present = connection.execute(
        f"SELECT name FROM sqlite_master WHERE name IN ({placeholders}) ORDER BY name", names,
    ).fetchall()
    if present:
        raise AssertionError(f"schema-13 fixture objects remain: {[row[0] for row in present]}")
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(work_units)")}
    if "current_intervention_id" in columns:
        raise AssertionError("schema-13 current intervention pointer remains")
