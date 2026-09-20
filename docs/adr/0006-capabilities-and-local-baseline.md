# ADR 0006: Capability-based integrations and local baseline

## Status

Accepted.

## Context

Projects should gain GitHub and Jira workflows without making remote credentials or services prerequisites for local work.

## Decision

Use capability-based adapters. Support local Git, Markdown work items, local artifacts, and configurable commands as the baseline. Implement GitHub and Jira as optional providers with test mocks and visible pending states. Let connector-capable Codex agents fulfill structured Jira requests. Never store credentials in project configuration or prompts.

## Consequences

Capability discovery and receipts become first-class workflow data. Missing remote access degrades visibly while eligible local work continues.
