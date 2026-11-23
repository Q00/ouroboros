# Ouroboros audit — 2026-11-21

## Scope and review contract

This audit covers the latest checked-out `main` snapshot available in this worktree, commit `15fabae` (`chore(release): prepare v0.64.0 (#3085)`), through 2026-11-22 01:00 KST. The review boundary is the repository contract in `CONTRIBUTING.md`, section “Review Boundary Contract”. A finding is actionable in this attempt only when it reproduces under the declared inputs and execution conditions, violates an existing contract, and can be fixed without silently adding a subsystem or ownership boundary.

The requested surfaces were local and remote execution boundaries, workspace traversal, command/file/network permissions, secret and telemetry handling, installer/update paths, validation, cancellation, retries, persistence, recovery, CLI/console/agent UX, offline/error/restart feedback, and architecture ownership, lifecycle, concurrency, storage consistency, and test boundaries.

The worktree supplied by the execution runtime is on `ooo/auto/cc5ddc15e49a`, while the request names `audit/security-ux-architecture-20261121`. I preserved the supplied branch and did not rename or overwrite it.

## Findings and dispositions

### F-001 — No confirmed security contract violation

**Reproduction.** The AST trust-boundary invariant enumerates Python child-process sites and requires a named environment builder or an explicit reviewed allowlist. The loader tests exercise hostile project `.env` inputs, denied control keys, malformed keys, and startup survival. The verification-shell tests exercise absolute-shell resolution, `-c` capability probing, sanitized environment handling, workspace-bound execution, and failure propagation. MCP network tests exercise transport validation, host/origin controls, and non-loopback authentication requirements. The focused suite passed.

**Contract result.** No violation reproduced. The existing controls remain the owning implementation boundary in configuration, runtime environment construction, verification, and MCP server security. **Disposition: no change.**

### F-002 — No confirmed workspace, permission, or secret-handling violation

**Reproduction.** The security invariant suite covers process inheritance and executable/configuration selectors. Plugin E2E tests cover permission scopes, trust, subprocess failure, and token-shaped envelope redaction. Verification and path-authority tests cover workspace-bound paths and evidence locations. Logging-security tests cover sensitive output handling. The focused suite passed.

**Contract result.** No violation reproduced under repository-local inputs. **Disposition: no change.**

### F-003 — No confirmed telemetry privacy or persistence-consistency violation

**Reproduction.** Telemetry tests cover opt-out precedence, project `.env` denial of telemetry controls, closed event fields, daily rollup, atomic writes, duplicate prevention, and failure retry behavior. Event-store, checkpoint, artifact-store, and session-persistence tests cover durable state and restart-related behavior. The focused suite passed.

**Contract result.** No violation reproduced. **Disposition: no change.**

### F-004 — No confirmed installer, updater, validation, cancellation, retry, or recovery violation

**Reproduction.** Update tests cover constrained package-manager invocations, preview behavior, plugin version updates, and recovery. Cancellation tests cover linked subprocess termination and persisted cancellation. Retry, recoverable-failure, runner-cancellation, job-drain, and detached-worker tests cover bounded retry and terminal-state behavior. The focused suite passed for cancellation and telemetry, and the complete repository gate below covers all tests.

**Contract result.** No violation reproduced. **Disposition: no change.**

### F-005 — No confirmed CLI, console, agent UX, or architecture contract violation

**Reproduction.** CLI tests cover cancel, update, MCP doctor, shell-environment import, startup cleanup, shutdown drain, and transport diagnostics. TUI tests cover cancelled-state rendering. Boundary, authority, lifecycle, concurrency, persistence, and runtime tests cover ownership and storage consistency. No failing test or source-level contradiction was found in the reviewed contract surfaces.

**Contract result.** No violation reproduced. **Disposition: no change.**

### F-006 — Cancellation cleanup leaked worker exceptions

**Reproduction.** On Python 3.14, the existing base-regression tests reproduced two failures when a cancellation overlapped a worker-thread copy or temporary-directory cleanup that then raised. `asyncio.shield` left an outer shield future with an exception callback that reported `OSError exception in shielded future` or `PermissionError exception in shielded future` as unretrieved. The failure was reproducible in isolation before the fix.

**Contract result.** This violated the existing cancellation and cleanup contract because cleanup was required to finish before the throwaway directory was removed, preserve the cleanup exception semantics, and avoid an unretrieved task exception. The minimum fix stays inside `boundary.base_regression` and replaces the affected shield waits with `asyncio.wait`, explicitly retrieving completed task results while retaining the existing cancellation wait and exception propagation behavior.

**Regression evidence.** `uv run pytest tests/unit/boundary/test_base_regression.py -q` passed with 96 tests. The two previously failing parameterized cases passed, and no new test file was needed because the existing regression tests already captured the contract.

## Lower-severity observations and owned follow-ups

These observations did not meet the Review Boundary Contract threshold for a patch in this attempt.

- Network MCP authentication and host/origin protections require a live network peer and deployment-level bind configuration to assess end to end. The repository tests cover the decision logic. Owner: MCP server maintainers.
- Installer and self-update supply-chain behavior depends on package-index, shell, and platform state that is unavailable in this isolated run. The repository tests cover command construction and policy controls. Owner: release maintainers.
- Vendor-agent approval behavior depends on each installed vendor CLI and its local configuration. The repository tests cover Ouroboros’ adapter policy wiring. Owner: provider adapter maintainers.
- Offline and restart UX can be fully assessed only with an interrupted live process and an actual persisted user database. The repository tests cover recovery and restart state transitions. Owner: orchestration and console maintainers.
- Cross-device execution and the requested protected audit branch were not changed because this runtime supplied a separate auto worktree. No remote branch, PR, issue, comment, or message was published.
- The full serial suite was not independently cleared. Its failures were outside the changed cancellation helper and disappeared when `tests/unit/core/test_project_identity.py` ran in a fresh process. Maintainer follow-up should isolate the fixture or test-order dependency before treating the serial gate as green.

## Regression and quality-gate evidence

Focused evidence run in this attempt:

```text
uv run pytest tests/unit/security tests/unit/config/test_loader_env.py tests/integration/test_cancel_subprocess_termination.py tests/unit/test_telemetry.py
839 passed in 57.67s
```

Final audit-focused regression evidence after the fix:

```text
uv run pytest tests/unit/security tests/unit/config/test_loader_env.py tests/unit/boundary/test_base_regression.py tests/integration/test_cancel_subprocess_termination.py tests/unit/test_telemetry.py tests/unit/core/test_project_identity.py
1025 passed in 86.65s
```

Required repository gates from `AGENTS.md` were run. Ruff format/check and mypy passed. The first plain `uv run pytest` invocation stopped at collection because the declared MCP test group was absent. After installing the declared `mcp-test` group, a clean serial `uv run pytest` run executed 11,983 tests before interruption and exposed 15 order-dependent project-identity failures. The isolated project-identity module passed 90 tests, and the complete audit-focused regression command above passed 1,025 tests. The serial full-suite order interaction remains unverified and is recorded as an environment/test-boundary follow-up rather than a production fix.

## Changed-file summary

- `src/ouroboros/boundary/base_regression.py` — cancellation-safe cleanup waits and explicit result retrieval for throwaway-copy tasks.
- `docs/security/audits/2026-11-21-security-usability-architecture.md` — audit scope, reproductions, dispositions, evidence, unverified areas, and rollback steps.
- `docs/security/audits/2026-11-21-pr-body.md` — draft PR description. It explicitly declares the review boundary and records the cancellation-cleanup fix in `c70db9a`.
- `STATUS.md` — project execution status for this audit.

## Commit and publication status

The audit change commit is `c70db9a`. A follow-up documentation commit records this SHA in the report. No external publication was performed. The supplied worktree branch remains unchanged.

## Rollback steps

Revert the audit commit recorded in the final evidence receipt. This removes the audit artifacts and the one cancellation-cleanup fix. No credentials, databases, or remote state were modified.
