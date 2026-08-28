# Workflow Failure Reasons Implementation

> Completed locally: 2026-08-13
> Branch: existing worktree branch

## Summary

Implemented privacy-safe failure classification across durable MCP job terminal
events and direct evaluation telemetry. Failed job results now carry a fixed
reason code, recovery action, and user-facing next step. Raw error text remains
internal to the job record and is not sent to PostHog.

## Files Created

| File | Purpose |
|---|---|
| `src/ouroboros/mcp/failure_taxonomy.py` | Closed enums and pure metadata/status classifier |
| `tests/unit/mcp/test_failure_taxonomy.py` | Classifier mapping and fail-closed tests |
| `docs/workflow-failure-reason/requirements.md` | Requirements and acceptance criteria |
| `docs/workflow-failure-reason/architecture.md` | Boundary/data-flow design |
| `docs/workflow-failure-reason/implementation.md` | This implementation record |

## Files Modified

| File | Changes |
|---|---|
| `src/ouroboros/mcp/job_manager.py` | Enrich every failed/cancelled/interrupted terminal event with fixed recovery metadata |
| `src/ouroboros/ralph_loop.py` | Carry Ralph's fixed stop-reason mappings into terminal tool metadata |
| `src/ouroboros/mcp/tools/evaluation_handlers.py` | Pass trusted typed failure reasons through direct evaluation telemetry |
| `src/ouroboros/telemetry.py` | Allowlist and emit `failure_reason_code`/`recovery_action` without raw text |
| `src/ouroboros/mcp/telemetry_boundary.py` | Apply the same enum contract to direct evaluation outcomes |
| `src/ouroboros/mcp/tools/job_handlers.py` | Render the reason, action, and next step for terminal jobs |
| `TELEMETRY.md` | Publish the exact property and privacy contract |
| `tests/unit/test_telemetry.py` | Cover each mapping, unknown fallback, and raw-data exclusion |
| `tests/unit/mcp/test_job_manager.py` | Update general-exception terminal contract |

## Key Implementation Details

`classify_failure()` accepts only terminal status and a mapping of structured
metadata. It returns `None` for successful/non-terminal states and always
returns a `FailureResolution` for a failure. All output strings are fixed
constants.

Ralph contributes only its fixed `action`/`stop_reason` vocabulary: actual
iteration or wall-clock exhaustion maps to `timeout`, while QA and convergence
guards (including `max_generations reached`) map to `validation`. Generic
`failed` remains `unknown`. Direct evaluation maps only typed errors such as
`ConfigError`, `ProviderError`, `MCPAuthError`, `MCPTimeoutError`, and
validation errors; generic `ValueError`/`RuntimeError` remains `unknown`.

`JobManager._append_event()` applies the resolution before persisting and before
`JobTelemetryBoundary.observe()` runs. This covers normal exceptions, shutdown,
cancellation, dead-owner recovery, stranded tasks, progress stalls, and future
terminal producers that use the same boundary.

`capture_job_outcome()` adds only the two enum fields to `workflow_outcome`.
`next_step`, raw `error`, `result_text`, and job IDs never enter telemetry.

## Testing

Focused verification:

```text
49 passed
```

Broader MCP/Ralph/telemetry verification:

```text
2190 passed, 1 skipped, 2 pre-existing failures
```

The two failures are in `tests/unit/mcp/test_routing_snapshot.py`; they use
the pre-existing unsupported `MCPServerAdapter(enable_routing_receipts=True)`
constructor argument and are unrelated to this feature. Ruff and mypy pass on
the changed source and tests.

Covered cases include successful evaluation regression, normal exception
fallback, cancellation/shutdown, timeout/stall, explicit config/auth/
validation codes, direct evaluation, exact allowlisting, deduplication, and
raw error/path exclusion.

## Known Limitations

- The implementation is local and not released yet.
- The seven-day reason-level recovery KPI cannot be reported until a complete
  post-release cohort exists.
- Unstructured provider failures and generic `ValueError`/`RuntimeError`
  remain `unknown`; adding a new category requires a structured metadata
  contract and corresponding tests.
