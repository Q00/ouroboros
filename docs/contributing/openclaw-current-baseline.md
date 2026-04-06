# OpenClaw Workflow Current Baseline

This note records the current verification baseline while the OpenClaw channel
workflow work is still in progress.

## What is green

The OpenClaw-focused scope currently passes targeted verification:

- `ruff` for the OpenClaw-related files
- `mypy` for `src/ouroboros/openclaw` and `channel_workflow_handler.py`
- targeted OpenClaw unit/integration suites

Examples of green scope:

- contracts
- command parsing
- workflow state / queue logic
- duplicate-delivery protection
- wait-driven orchestration
- transport bridge
- MCP server wiring for `ouroboros_channel_workflow`

## What is not yet green

The full repository-wide test suite is not currently all-green.
When last checked, the failing set was dominated by existing non-OpenClaw areas such as:

- orchestrator runner / cancellation
- full workflow e2e
- session persistence e2e
- unrelated event tests present in the working tree

These failures should not be interpreted as evidence that the OpenClaw workflow
scaffold itself is broken. They indicate that the repository baseline is broader
than the current OpenClaw change set.

## Recommended verification posture for this work

Until the broader suite is stabilized, treat this as the primary confidence bar:

1. targeted OpenClaw tests must remain green
2. ruff + mypy on OpenClaw-related files must remain green
3. transport-facing contracts and metadata shape should stay stable

## Why this note exists

This project currently has a mismatch between:

- "OpenClaw-specific work is passing"
- "the entire repository test suite is green"

Writing that down explicitly reduces confusion during ongoing iteration.
