# Runtime delegation transition contract (#925)

This RFC fixes the boundary between plugin-delegated work and JobManager-owned
background jobs. It is intentionally a documentation/test slice: it does not
rewrite Ralph, plugin, MCP, or scheduler runtime behavior.

## Scope taxonomy

| Scope | Owner | Subject id | Terminal authority | Cancellation/polling surface |
|---|---|---|---|---|
| `mcp_job` | `JobManager` | concrete `job_id` | `JobManager` terminal job event/result | `ouroboros_get_job_status`, `ouroboros_cancel_job` |
| `plugin` | plugin firewall/bridge delegated child session | plugin or delegated session id | plugin bridge/firewall audit evidence, not a job terminal row | runtime-specific child-session controls; job tools do not apply |
| `harness_runner` | local harness runner | execution/session id | harness execution state | execution cancellation/status tools |
| `ralph` / `auto` / `team` | owning workflow controller | workflow/session id | workflow controller state | workflow-specific controls |

A plugin delegation MUST NOT fabricate a `JobManager` job id merely to reuse
job polling. If the bridge returns `status=delegated_to_plugin` and `job_id=None`,
callers must treat that run as `runtime_scope=plugin` until a real owner emits
separate evidence. Conversely, a real background job MUST use
`runtime_scope=mcp_job` and the concrete `job_id` as its subject.

## Transition evidence rules

`RuntimeTransition` records the narrow boundary decision before a caller mutates
runtime state. The event data must include `runtime_scope`, `subject_id`,
`from_state`, `to_state`, `actor`, and bounded metadata. Terminal transitions
require at least one `evidence_refs` entry by default when the caller supplies
the terminal-state set to the validator. Callers may also opt in for additional
cross-boundary changes before the terminal state. Evidence must point at the
owner surface:

- `mcp_job`: `event://mcp.job.status/<job_id>` or equivalent JobManager event.
- `plugin`: `event://plugin/<plugin_id>/delegated/<session_id>` or plugin audit
  evidence from the bridge/firewall.
- `harness_runner`: harness execution event or artifact reference.

Missing evidence is a blocking contract failure, not a retryable polling issue.
Stale revisions and snapshot drift remain retryable because the caller can reload
state and re-evaluate. Existing terminal states remain terminal failures.

## Plugin delegation vs JobManager non-goals

This contract does **not**:

1. move plugin child sessions into `JobManager`;
2. add a scheduler for plugin-delegated work;
3. change Ralph loop ownership;
4. add plugin permission prompts; or
5. reinterpret `JobManager` cancellation as plugin child-session cancellation.

Future runtime work may build adapters on top of this boundary, but must keep
plugin-delegated sessions and JobManager jobs distinguishable in persisted
audit/event data.
