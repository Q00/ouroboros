## Ouroboros Skill Capability Guide: Claude

### When a skill requires `ask_user`
Use the runtime's native structured question surface when available; otherwise ask one concise question and wait.

### When a skill requires `inspect_code`
Use the runtime's local file search/read tools and prefer exact repository evidence over inference.

### When a skill requires `call_mcp`
Call available Ouroboros MCP tools through the runtime's MCP/tool surface instead of emulating MCP workflows manually.

### When a skill requires `run_lateral_review`
When an interview response marks `lateral_review_required=true`, call `ouroboros_lateral_think` with the supplied `lateral_review_tool_args` before routing the next interview turn. When directly synthesizing an answer for the user, run researcher, contrarian, and simplifier perspectives first, then collapse the result into concise choices or a recommended draft.

### When a skill requires `web_research`
Use the runtime's web/search capability only when current external facts are required, and cite the sources used.

### When a skill requires `run_shell`
Use the runtime's bounded local shell capability for safe repository/version checks; avoid destructive commands unless explicitly authorized.

### When a skill requires `refine_answer`
Confirm structured interpretations of free-text decisions before forwarding them to workflow state.

### When a skill requires `maintain_ledger`
Keep ambiguity, gates, and unresolved decisions visible in the main session rather than hiding them only in tool state.

### When a skill requires `run_closure_gate`
Audit required client-side gates even when an MCP response says the workflow is ready to proceed.

### When a skill requires `restate_goal`
Restate the goal and require explicit approval before irreversible workflow transitions such as seed generation.
