---
name: cancel
description: "Cancel stuck or orphaned executions"
aliases: [kill, abort]
---

# /ouroboros:cancel

Cancel stuck or orphaned executions and background jobs through the Ouroboros MCP tools, which work on hosts without shell access.

```
/ouroboros:cancel                 # list this project's active executions, ask which to cancel
/ouroboros:cancel <id>            # cancel one execution (orch_... or execution id) or job (job_...)
/ouroboros:cancel --all           # cancel every running or paused execution of this project
```

## Instructions

### Loading MCP tools (deferred-schema guard)

Ouroboros tools may be deferred, and a schema loaded on one turn can unload by the next. Immediately before each `ouroboros_*` call, load it with the active runtime's tool-discovery capability: `"+ouroboros project status"` for `ouroboros_project_status`; `"+ouroboros cancel"` for `ouroboros_cancel_execution` and `ouroboros_cancel_job`. A call whose schema is not loaded fails with "Invalid tool parameters". If the tool is already callable, discovery is a no-op and an empty result is expected. Use the fallback only when the tool is genuinely absent.

### Listing active executions

Call `ouroboros_project_status` with `project_dir` set to the absolute path of the current project. Active executions are the runs whose `status` is `running` or `paused`; show each one's `session_id`, `started_at`, and `seed_goal`. If the call fails because the project has more runs than `limit`, retry once with `limit` set to the reported run count. The listing covers only runs attributed to this project. Background jobs have no listing tool, so also offer any `job_...` id returned by a start tool earlier in this conversation.

### Steps

- **No ID, not "all"**: list as above, ask which to cancel with the active runtime's `ask_user` capability, then cancel the chosen ids as below. If nothing is active, say so (and, when `run_shell` is available, mention `ouroboros cancel execution --list` for executions outside this project). If the user declines, cancel nothing.
- **ID given**: an id starting with `job_` is a background job: call `ouroboros_cancel_job` with `job_id`. Any other id (an `orch_...` session id or an execution id) is an execution: call `ouroboros_cancel_execution` with `execution_id` and, if the user gave one, `reason`.
- **"cancel all" / "cancel everything"**: there is no bulk cancel tool. List as above and call `ouroboros_cancel_execution` once per running or paused run. Say this covered this project's runs only; for every execution on the machine, `ouroboros cancel execution --all` needs `run_shell`.

### Reporting outcomes

- `ouroboros_cancel_execution`: `new_status` `cancelled` means it is cancelled. `cancellation_requested` means the live worker was asked to stop and will record the cancellation itself; suggest `ooo status` to confirm. An "already in terminal state" error means there was nothing to cancel; skip it with a warning. "Execution not found" means the id is wrong. "Held by another live owner" means only the process that owns it can cancel it; relay that verbatim. "Failed to persist cancellation" is retriable; retry once.
- `ouroboros_cancel_job`: `status` `cancel_requested` means the job and its linked execution were asked to stop; `cancelled` means done. Any other terminal status (`completed`, `failed`, `interrupted`) means the job had already finished and nothing was cancelled. "Job not found" means the id is wrong.

Relay any other error verbatim. Then suggest `ooo status` to verify or `ooo run` to start fresh.

## Fallback (no MCP server)

When the cancel tools are genuinely absent and the active runtime has `run_shell`, use the CLI, which writes cancellation events straight to the event store: `ouroboros cancel execution --list` (prints active executions without prompting; then ask with `ask_user`), `ouroboros cancel execution <session_id>`, or `ouroboros cancel execution --all`. Add `--reason "<text>"` to record why, and report what it printed (`Cancelled execution: <id>`, `Cancellation requested for execution: <id>`, or `No active executions found.`) with any warning verbatim. Without `run_shell`, tell the user cancellation needs the Ouroboros MCP server (`ooo setup`) or a shell.

## RFC #1392 State Breadcrumb Footer

End the final response with exactly one line `◆ <current state> → next: <recommended action>`. Derive the state from live session state via `ouroboros_session_status` when available, otherwise from this skill's actual outcome. Never use a linear `Step N of M` footer; when the next step is a real choice, list 2-3 options.
