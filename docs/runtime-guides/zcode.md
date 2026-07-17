# Zcode CLI Runtime

Run Ouroboros workflows through Z.ai's locally installed ZCode coding agent.
The runtime supports either the macOS app-bundle `zcode.cjs` entry script or a
`zcode` executable available on `PATH`.

> The vendor does not currently publish a stable CLI or JSON-output contract.
> This adapter is pinned to behavior measured from Zcode CLI 0.15.0 and 0.15.2
> and to the captured fixtures under `tests/fixtures/zcode/`. Treat ZCode
> application upgrades as compatibility events and rerun the Zcode runtime
> tests after an upgrade.

## Prerequisites

| Requirement | Why |
| --- | --- |
| ZCode desktop app or `zcode` executable | Provides the coding-agent runtime |
| A configured Z.ai provider/model | Zcode reads provider and model selection from its own config |
| Compatible Node.js for a standalone `.cjs` path | Official app bundles use their bundled Electron/Node runtime; standalone scripts use the system Node |
| Ouroboros base package | No provider-specific Python extra is required |

## Quick start

```bash
ouroboros setup --runtime zcode
ouroboros run workflow seed.yaml --runtime zcode
```

If setup cannot find ZCode automatically, configure one of:

```bash
export OUROBOROS_ZCODE_CLI_PATH=/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs
```

```yaml
orchestrator:
  runtime_backend: zcode
  zcode_cli_path: /Applications/ZCode.app/Contents/Resources/glm/zcode.cjs
```

## CLI path resolution

The runtime resolves the CLI in this order:

1. Constructor argument `cli_path=...`
2. `OUROBOROS_ZCODE_CLI_PATH`
3. `orchestrator.zcode_cli_path` in `~/.ouroboros/config.yaml`
4. `/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs`
5. `zcode` on `PATH`

Official app bundles include `.node-bundle-meta.json` with
`runtime: electron-node`. Ouroboros reads that metadata, launches
`ZCode.app/Contents/MacOS/ZCode`, and sets `ELECTRON_RUN_AS_NODE=1`, matching
ZCode's own launcher. A configured script inside a `.app` bundle fails closed
when the metadata, plist, or bundled executable is missing or invalid; it never
falls back to an unrelated system Node. Standalone `.cjs`, `.js`, or `.mjs`
paths use the system Node.js. Other paths are treated as executable wrappers or
binaries and are invoked directly. `NODE_OPTIONS` is removed from both Node
launch shapes so a project or parent process cannot preload JavaScript into the
vendor CLI.

## Runtime-only backend

Zcode drives agentic execution but is not an Ouroboros LLM-completion backend.
Keep a completion-capable value such as `claude_code`, `codex`, or `litellm` in
`llm.backend` for interview, seed generation, evaluation, and QA handlers.
Direct `ZcodeCLIRuntime` construction defaults those auxiliary calls to
`claude_code`, matching the other runtime-only adapters.

## Headless contract

For an official macOS app-bundle script, each task uses this command shape:

```text
ELECTRON_RUN_AS_NODE=1 <ZCode.app/Contents/MacOS/ZCode> <zcode.cjs> \
  --json \
  --prompt <PROMPT> \
  --mode <edit|yolo> \
  [--cwd <PATH>] \
  [--resume <SESSION_ID>]
```

The measured `--json` behavior is one pretty-printed summary object emitted at
the end of the turn, not an NDJSON event stream. The runtime reassembles stdout
before parsing it and maps the top-level `response` to one terminal assistant
message. The top-level `sessionId` becomes the resume handle.

## Live compatibility evidence

The official notarized macOS ARM packages were exercised end to end with a
local OpenAI-compatible model endpoint, so the vendor process, streaming model
adapter, JSON summary, persisted session, and `--resume` path were all real:

| ZCode app | CLI | Result |
| --- | --- | --- |
| 3.2.5 | 0.15.0 | Headless JSON response and same-session resume passed |
| 3.3.5 | 0.15.2 | Headless JSON response and same-session resume passed |

Both bundles declare `runtime: electron-node` and include Electron 41 / Node
24. Running their `zcode.cjs` directly with Node 20 fails on the vendor's
`node:sqlite` import; the app-bundle launcher selection above prevents that
environment-dependent failure.

The adapter does not emit `--non-interactive`, `--approval-mode`, or `--model`.
Those are not accepted Zcode 0.15.0 or 0.15.2 flags. Model selection remains in
Zcode's own configuration, including `~/.zcode/cli/config.json` `model.main`.

## Permission mapping

| Ouroboros mode | Zcode `--mode` | Behavior |
| --- | --- | --- |
| `acceptEdits` | `edit` | Default non-interactive edit mode |
| `bypassPermissions` | `yolo` | Explicit full bypass |
| `default` | `edit` | Normalized to the safe non-interactive default |

## Buffered-output timeout behavior

Zcode stays silent until its final JSON summary is ready. The inherited
60-second first-output watchdog is therefore disabled by default because it
would otherwise become a 60-second total-task limit. Callers that require a
no-output deadline can pass `startup_output_timeout_seconds` explicitly.

This is an operational tradeoff: without an explicit outer deadline, a vendor
process that never emits its summary can remain pending. Production callers
should retain their workflow-level deadline or configure a suitable startup
output timeout for their expected task duration.

## Capabilities and limits

| Capability | Status |
| --- | --- |
| Headless execution | Yes, via `--prompt --json` |
| Structured final output | Yes, one summary object per turn |
| Intermediate tool events | No, not present in measured stdout |
| Targeted session resume | Yes, via `--resume <sessionId>` |
| Per-call model override | No, model selection is Zcode-owned |
| LLM-completion backend | No, runtime-only |
| Setup-owned instruction artifact | No, capability guide rendering is the fallback |

## Troubleshooting

**Zcode is not detected.** Set `OUROBOROS_ZCODE_CLI_PATH`, configure
`orchestrator.zcode_cli_path`, or put a `zcode` executable on `PATH`.

**A standalone script reports a missing Node built-in such as `node:sqlite`.**
Use the intact ZCode app-bundle path so Ouroboros can select the bundled
Electron/Node runtime, install a Node version compatible with that Zcode build,
or configure a directly executable `zcode` wrapper.

**A model override has no effect.** Zcode has no per-invocation `--model`
flag. Select the model in ZCode's own provider/model configuration.

**Parsing breaks after a ZCode update.** Run
`tests/unit/orchestrator/test_zcode_cli_runtime.py` and recapture the vendor
summary fixture before changing the parser contract.
