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
| `ooo status [session_id]` | `ouroboros_session_status` |
| `ooo evaluate <session_id>` | `ouroboros_evaluate` |
| `ooo evolve ...` | `ouroboros_evolve_step` |
| `ooo cancel [execution_id]` | `ouroboros_cancel_execution` |
| `ooo unstuck` / `ooo lateral` | `ouroboros_lateral_think` |

## Natural Language Mapping

For natural-language requests, map to the corresponding MCP tool:
- "clarify requirements", "interview me", "socratic interview" → call `ouroboros_interview`
- "generate a seed", "freeze requirements" → call `ouroboros_generate_seed`
- "run the seed", "execute the workflow" → call `ouroboros_execute_seed`
- "check status", "am I drifting?" → call `ouroboros_session_status`
- "evaluate", "verify the result" → call `ouroboros_evaluate`

## Setup & Update

- `ooo setup` → install or refresh Ouroboros Codex and MCP artifacts
- `ooo update` → upgrade Ouroboros and refresh installed Codex artifacts

If the request is clearly unrelated to Ouroboros, handle it normally.
