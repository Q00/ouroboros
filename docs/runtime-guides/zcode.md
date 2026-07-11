# Zcode Runtime

Run Ouroboros workflow execution and LLM-backed authoring through the locally
installed Zcode CLI.

Zcode integration has two separate surfaces:

1. **Terminal path**: Ouroboros launches Zcode from the shell for runtime
   execution and, when selected, LLM completion roles.
2. **GUI path**: Zcode GUI launches Ouroboros through MCP. This is a separate
   plugin packaging path and is not proven by the terminal smoke test below.

This guide covers the terminal path.

## Mental Model

```text
Terminal / CLI / MCP client
      |
      | selects runtime_backend: zcode or llm.backend: zcode
      v
Ouroboros runtime/provider adapter
      |
      | shells out to zcode --prompt --json --mode <mode> --cwd <cwd>
      v
Zcode CLI
      |
      | uses Zcode's own auth, model config, tools, and session state
      v
GLM/Z.ai model turn
```

Ouroboros owns Seed parsing, workflow orchestration, evaluation handoff, event
storage, and backend selection. Zcode owns model access, model selection, and
the actual agent turn.

## Prerequisites

| Requirement | Why |
| --- | --- |
| ZCode.app or `zcode` CLI | Provider runtime |
| Z.ai login | Zcode needs its own authenticated model access |
| Ouroboros base package | Provides `ouroboros` CLI and adapters |

On macOS app-bundle installs, the CLI entry script is typically:

```bash
/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs
```

Point Ouroboros at it:

```bash
export OUROBOROS_ZCODE_CLI_PATH=/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs
```

If `zcode` is already on `PATH`, the explicit env var is optional.

## Model Selection

Zcode has no `--model` CLI flag. Passing `--model` is a hard CLI rejection.

Select the model through Zcode itself, for example:

```text
~/.zcode/cli/config.json  ->  model.main
```

or from an interactive Zcode session using `/model`.

Ouroboros intentionally does not forward `CompletionConfig.model` or runtime
model overrides to the Zcode CLI. The Zcode runtime emits a warning if a
non-default model is requested so the mismatch is visible.

## Quick Start: Runtime Execution

Use Zcode as the execution runtime:

```bash
export OUROBOROS_AGENT_RUNTIME=zcode
export OUROBOROS_ZCODE_CLI_PATH=/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs

ouroboros run workflow seed.yaml --runtime zcode
```

That command is the intended workflow entry point once a Seed is available. The
smoke test below proves the lower-level terminal contract that this path depends
on: Ouroboros can construct a Zcode runtime and execute a real Zcode task from a
shell process.

For a local smoke test against the real Zcode CLI:

```bash
export OUROBOROS_ZCODE_SMOKE=1
export OUROBOROS_ZCODE_CLI_PATH=/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs

SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0 \
  uv run --python 3.13 python -m pytest tests/integration/test_zcode_cli_smoke.py -q
```

Run it from an Ouroboros checkout or another environment where the
`ouroboros-ai` package is installed in editable/development mode.

The smoke test is skipped unless `OUROBOROS_ZCODE_SMOKE=1` is set, so regular
CI does not require Zcode credentials or network access. When enabled, it proves
two terminal contracts:

- `create_agent_runtime(backend="zcode")` can execute a real Zcode task.
- `ZcodeCliLLMAdapter` can satisfy a structured `json_object`
  `CompletionConfig.response_format` through the real Zcode CLI.

## Quick Start: LLM Roles

Use Zcode for completion-style LLM roles:

```bash
export OUROBOROS_LLM_BACKEND=zcode
export OUROBOROS_ZCODE_CLI_PATH=/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs

ouroboros qa ./some-artifact.txt \
  --artifact-type document \
  --quality-bar "PASS if the artifact is internally consistent."
```

The adapter requests `zcode --prompt --json`, parses Zcode's single JSON
summary, and returns the top-level `response` field as the completion text. For
structured output callers, it injects a JSON-only directive, extracts the JSON
payload, validates `json_object` / `json_schema` requirements, and retries
non-conforming responses before returning an error.

## Runtime Contract

For a normal execution task, Ouroboros launches:

```text
zcode --json --prompt <PROMPT> --mode <edit|yolo> [--cwd <cwd>] [--resume <sessionId>]
```

When `OUROBOROS_ZCODE_CLI_PATH` points to a `.cjs`, `.js`, or `.mjs` script,
Ouroboros invokes it as:

```text
node <path-to-zcode.cjs> --json --prompt <PROMPT> ...
```

| Argument | Why |
| --- | --- |
| `--json` | Requests Zcode's machine-readable summary object |
| `--prompt` | Runs one non-interactive prompt without opening the TUI |
| `--mode` | Maps Ouroboros permissions: `acceptEdits` -> `edit`, `bypassPermissions` -> `yolo` |
| `--cwd` | Runs Zcode from the selected project directory |
| `--resume` | Resumes a prior Zcode session when a runtime handle provides one |

Zcode emits one buffered JSON summary at completion instead of a continuous
NDJSON stream. For that reason the Zcode runtime disables the inherited
first-output watchdog by default; otherwise a healthy long run could be killed
before Zcode prints its final summary.

## Terminal vs GUI Success

The terminal smoke test proves that Ouroboros can drive Zcode from the shell.
It does not prove that the Zcode GUI can drive Ouroboros.

The GUI path requires a Zcode plugin/MCP package that lets the Zcode GUI start
`ouroboros mcp serve` and call tools such as `ouroboros_interview`,
`ouroboros_execute_seed`, and `ouroboros_qa`. That is the next integration
step after the terminal path is stable.
