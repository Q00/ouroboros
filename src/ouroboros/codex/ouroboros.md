# Ouroboros for Codex

Use Ouroboros commands when the user is asking to clarify requirements, generate a seed, run a seed, inspect workflow status, evaluate an execution, or manage Ouroboros setup.

## CRITICAL: MCP Tool Routing

When the user types `ooo <command>`, you MUST call the corresponding MCP tool.
Do NOT interpret `ooo` commands as natural language. ALWAYS route to the MCP tool.

| User Input | MCP Tool to Call |
|-----------|-----------------|
| `ooo interview "<topic>"` | `ouroboros_interview` with `initial_context` |
| `ooo interview "<answer>"` (follow-up) | `ouroboros_interview` with `answer` and `session_id` |
| `ooo seed [session_id]` | `ouroboros_generate_seed` |
| `ooo run <seed.yaml>` | `ouroboros_execute_seed` with `seed_path` |
| `ooo auto ...` | `ouroboros_start_auto` with the resolved `goal` / `resume` / option arguments |
| `ooo status [session_id]` | `ouroboros_session_status` |
| `ooo evaluate <session_id>` | `ouroboros_evaluate` |
| `ooo evolve ...` | `ouroboros_evolve_step` |
| `ooo cancel [execution_id]` | `ouroboros_cancel_execution` |
| `ooo unstuck` / `ooo lateral` | `ouroboros_lateral_think` |
| `ooo auto ...` | `ouroboros_start_auto` |

If `ouroboros_start_auto` is unavailable, stop and report that the MCP dispatch surface is broken. Do not manually emulate `ooo auto` with ordinary shell, GitHub, or coding work.

## Natural Language Mapping

For natural-language requests, map to the corresponding MCP tool:
- "clarify requirements", "interview me", "socratic interview" → call `ouroboros_interview`
- "generate a seed", "freeze requirements" → call `ouroboros_generate_seed`
- "run the seed", "execute the workflow" → call `ouroboros_execute_seed`
- "check status", "am I drifting?" → call `ouroboros_session_status`
- "evaluate", "verify the result" → call `ouroboros_evaluate`

## Auto Dispatch Safety

`ooo auto` has a strict product contract: bounded interview, Seed generation,
A-grade review/repair, and execution handoff. Do not emulate it with manual
shell, repository, or GitHub work.

If a user input starts with `ooo auto`, call `ouroboros_start_auto`. Full auto
runs routinely exceed interactive MCP tool-call timeouts, so the background
starter is the supported default. It returns `job_id` and `auto_session_id`
quickly; report both briefly, retain the `job_id` plus latest cursor, and keep
monitoring the job yourself with `ouroboros_job_wait` / `ouroboros_job_status`.
Do not hand the user polling instructions as the final UX. For normal
conversational tracking, call `ouroboros_job_wait` with a positive
`timeout_seconds` value (for example 120) and `view="summary"`, update the cursor
from `response.meta.cursor`, relay only meaningful changes, and continue until a
terminal job status is reached or the user explicitly asks you to stop. If that
MCP tool is unavailable, stop and report that `ouroboros_start_auto` is
unavailable instead of continuing as a normal Codex task.

If `ouroboros_auto` is invoked and returns an auto-session outcome such as
`blocked`, `failed`, or `complete`, report that outcome as the auto session
result. `detached` is non-terminal tracked background work; surface the job or
Ralph handles and keep polling without blocking the foreground tool call. After
the auto job reaches a terminal job status, call `ouroboros_job_result(job_id)`
and summarize the final auto result. Do not call a `blocked` or `failed`
auto-session result a dispatch failure; dispatch failure is reserved for cases
where the MCP tool could not be invoked.

## Setup & Update

- `ooo setup` → write Ouroboros config (`~/.ouroboros/config.yaml`) and register the MCP server
- `ooo update` → upgrade Ouroboros to the latest PyPI version

If the request is clearly unrelated to Ouroboros, handle it normally.
