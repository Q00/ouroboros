## Ouroboros Skill Capability Guide: Claude

### When a skill requires `ask_user`
Use the runtime's native structured question surface when available; otherwise ask one concise question and wait.

### When a skill requires `inspect_code`
Use the runtime's local file search/read tools and prefer exact repository evidence over inference.

### When a skill requires `call_mcp`
Call available Ouroboros MCP tools through the runtime's MCP/tool surface instead of emulating MCP workflows manually.

### When a skill requires `refine_answer`
Confirm structured interpretations of free-text decisions before forwarding them to workflow state.

### When a skill requires `run_closure_gate`
Audit required client-side gates even when an MCP response says the workflow is ready to proceed.

### When a skill requires `restate_goal`
Restate the goal and require explicit approval before irreversible workflow transitions such as seed generation.
