# Shared `ooo` Skill Dispatch Router

This guide describes the runtime-facing dispatch path used by Codex CLI,
Hermes, and OpenCode after setup installs the packaged Ouroboros skills.

## Setup Contract

Run the runtime-specific setup command once:

```bash
ouroboros setup --runtime codex
ouroboros setup --runtime hermes
ouroboros setup --runtime opencode
```

Setup installs the packaged `skills/*/SKILL.md` files for the selected runtime
and registers the normal Ouroboros MCP server. After that, supported runtimes
can intercept exact skill commands before starting their model/subprocess flow:

```text
ooo run seed.yaml
/ouroboros:ouroboros-run seed.yaml
```

The shared router lives in `ouroboros.router`. It owns the deterministic
resolution pipeline:

1. Parse exact `ooo <skill>` and `/ouroboros:<skill>` command prefixes.
2. Resolve the skill name or alias against packaged `SKILL.md` files.
3. Load and validate `mcp_tool` / `mcp_args` frontmatter.
4. Substitute supported templates in `mcp_args` (`$1` and `$CWD`).
5. Return runtime-neutral dispatch metadata.

## Runtime Boundary

The router is stateless and performs no logging. It does not assemble
`AgentMessage` objects, invoke MCP handlers, inspect channel metadata, or infer
entry points from free-form text.

Codex CLI, Hermes, and OpenCode remain responsible for:

- caller-observable structured logging
- runtime-specific `AgentMessage` assembly
- invoking the configured or built-in MCP handler named by `mcp_tool`
- falling through to the normal runtime path for non-dispatch prompts

The MCP server remains unaware of `ooo` syntax and channel identifiers. It only
exposes ordinary MCP tools such as `ouroboros_execute_seed`,
`ouroboros_pm_interview`, and `ouroboros_evaluate`.

## Adding Commands

Adding or changing an `ooo` dispatch command is a `SKILL.md`-only change. Do not
add runtime parser branches or MCP server special cases.

Example frontmatter:

```yaml
---
name: ouroboros-run
description: Execute a Seed specification through the workflow engine
mcp_tool: ouroboros_execute_seed
mcp_args:
  seed_path: "$1"
  cwd: "$CWD"
---
```

Aliases are also declared in skill metadata, so runtimes pick them up through
the same shared router without code changes. The containing directory remains
the canonical runtime identity (`skills/run` in this example); the frontmatter
name is the host-facing registration name.
