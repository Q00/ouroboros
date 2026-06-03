# Pi CLI Runtime

Run Ouroboros workflow execution on top of the locally installed
[`pi`](https://github.com/earendil-works/pi) CLI.

The Pi runtime is a subprocess adapter. Ouroboros owns the workflow engine,
Seed decomposition, checkpointing, evaluation handoff, and `ooo` skill
dispatch. For each runtime task it shells out to Pi JSON mode and normalizes
Pi's JSONL events into Ouroboros `AgentMessage` values.

## Mental Model

There are three separate layers:

```text
User / CLI / MCP
      |
      | 1. Selects runtime_backend: pi, or sends an ooo shortcut
      v
Ouroboros runtime adapter
      |
      | 2a. ooo shortcut? handle inside Ouroboros before Pi starts
      | 2b. normal task? spawn Pi JSON mode
      v
pi --mode json <prompt>
      |
      | 3. Pi loads its own settings, packages, extensions, tools, model auth
      v
Pi model turn and JSONL events
```

So "Pi is an Ouroboros runtime" means step 2b exists and is selectable. It
does not mean Pi packages are imported into Ouroboros, and it does not mean
Pi's interactive command UI becomes part of the Ouroboros command router.

## Prerequisites

| Requirement | Why |
|-------------|-----|
| `pi` CLI | Provider runtime; install Pi and keep `pi` on `PATH`, or configure an explicit path |
| Pi auth | Run the Pi provider login flow before first use |
| Ouroboros base package | `pip install ouroboros-ai` |

For OpenAI subscription-backed Codex models in Pi, use Pi's `openai-codex`
provider login/model path. The ordinary `openai` provider path is API-key
oriented and is not the same auth surface.

## Quick Start

```bash
# 1. Install and authenticate Pi
npm install -g --ignore-scripts @earendil-works/pi-coding-agent
pi login openai-codex

# 2. Point Ouroboros at Pi
ouroboros setup --runtime pi

# 3. Run a workflow through the configured runtime
ouroboros run workflow seed.yaml
```

If Pi is installed outside `PATH`, set:

```bash
export OUROBOROS_PI_CLI_PATH=/absolute/path/to/pi
```

or configure:

```yaml
orchestrator:
  runtime_backend: pi
  pi_cli_path: /absolute/path/to/pi
```

## Runtime Contract

For a normal execution task, Ouroboros launches:

```text
pi --mode json [--model <MODEL>] [--session <SESSION_ID>] <PROMPT>
```

| Argument | Why |
|----------|-----|
| `--mode json` | Requests Pi's headless JSONL event stream |
| `--model` | Optional model override passed by the caller |
| `--session` | Optional native Pi session id for targeted resume |
| `<PROMPT>` | The composed task prompt from Ouroboros |

Ouroboros parses the initial `session` event into a `RuntimeHandle`, streams
`message_update` `text_delta` events as assistant output, and reads terminal
assistant text from `message_end`, `turn_end`, or `agent_end` events.

Pi may report provider/model failures as assistant messages with
`stopReason: "error"` while the process still exits with status `0`.
Ouroboros treats those events as runtime errors instead of relying only on the
process return code.

## What `ooo` Means With Pi

`ooo <skill>` is handled by Ouroboros before the Pi subprocess starts.

The Pi runtime calls the shared `SkillInterceptor` at the top of
`PiRuntime.execute_task()`. If the prompt is an Ouroboros skill shortcut such
as `ooo interview` or `/ouroboros:run`, the interceptor resolves the skill and
invokes the matching Ouroboros MCP handler. Pi does not receive that prompt as
ordinary chat input.

This means:

- `ooo interview` in an Ouroboros-controlled Pi runtime means "Ouroboros
  handles the interview command, using the configured LLM backend for
  authoring."
- It does not mean Pi has learned a new native `ooo` slash command.
- It does not mean typing `ooo interview` inside an unrelated interactive Pi
  terminal session will invoke Ouroboros.

## `ooo auto --runtime pi`

`ooo auto` has two different completion levels:

| Command shape | What completes | Pi involvement |
|---------------|----------------|----------------|
| `ouroboros auto --runtime pi ...` | Interview, Seed generation, Seed QA, and run handoff | Starts an execution handoff for the Pi runtime; the final product may still be pending |
| `ouroboros auto --runtime pi --complete-product ...` | Interview, Seed generation, Seed QA, Pi execution, and product completion | Runs `ouroboros_execute_seed` inline and waits for Pi-backed AC execution to finish |

Use `--complete-product` when you need a foreground/manual smoke test that
proves Pi actually executed the Seed task before the command exits.

Pi model selection comes from Pi's own default unless the execution path passes
a model override. For reproducible smoke tests, set:

```bash
export OUROBOROS_EXECUTION_MODEL=openai-codex/gpt-5.4-mini
```

Then the Pi runtime launch includes:

```text
pi --mode json --model openai-codex/gpt-5.4-mini <PROMPT>
```

Auto usually runs in Ouroboros-managed task worktrees. A successful file-writing
smoke test may therefore create files under `~/.ouroboros/worktrees/...` rather
than in the shell's original checkout.

## Pi Packages And roach-pi

Pi packages and extensions are loaded by Pi itself. If a user has installed a
package such as `git:github.com/tmdgusya/roach-pi` in Pi's own settings, the
Pi subprocess launched by Ouroboros can load that package through Pi's normal
extension loader.

That is different from saying `roach-pi` is the Ouroboros runtime adapter.

| Scenario | Contract |
|----------|----------|
| Ouroboros launches `pi --mode json` | Supported Pi runtime path |
| Pi loads installed packages/extensions during that process | Allowed by Pi; visible to the Pi run |
| A package adds headless-compatible tools/hooks used by Pi's model turn | May work, because it runs inside Pi |
| A package adds interactive slash commands or UI prompts | Not guaranteed by the Ouroboros Pi runtime |
| `roach-pi` becomes a selectable Ouroboros runtime backend by itself | No; that would require a dedicated adapter or bridge |

In short: `runtime_backend: pi` selects the Pi CLI as the execution engine.
A customized Pi distribution can affect what happens inside that Pi process,
but Ouroboros only depends on Pi's JSON-mode subprocess contract.

## Pi As LLM Backend

Pi can also be selected as an LLM backend for unstructured completion flows:

```yaml
llm:
  backend: pi
```

This is separate from `orchestrator.runtime_backend`.

The Pi LLM adapter supports structured `response_format` requests through soft
enforcement: Ouroboros injects a strict JSON/schema instruction, extracts the
JSON payload from Pi's response, and validates `json_schema` payloads before
returning them. Pi JSON mode does not currently expose a Codex-style native
`--output-schema` hard-enforcement flag, so malformed structured responses are
retried and then surfaced as provider errors.

Use Pi as the runtime backend when you want Pi to execute Seed tasks; use
`llm.backend: pi` when the authoring/evaluation flow can accept adapter-level
JSON extraction and validation rather than provider-native schema enforcement.

## Capabilities

| Capability | Status |
|------------|--------|
| Headless execution | Yes, through `pi --mode json` |
| Skill shortcut dispatch | Yes, before spawning Pi |
| Native targeted resume | Yes, through `--session <id>` |
| Structured event stream | Yes, JSONL parsed by `PiRuntime` |
| Structured schema responses as LLM backend | Soft-enforced and validated |
| Pi extension loading | Pi-owned; works when compatible with headless JSON mode |
| Interactive Pi slash UI under Ouroboros | Not part of the runtime contract |

## Troubleshooting

**`Pi not found`**
Install Pi, put `pi` on `PATH`, or set `OUROBOROS_PI_CLI_PATH`.

**OpenAI OAuth works in Pi but Ouroboros still fails**
Check the model string. For subscription-backed OpenAI Codex models in Pi,
use the `openai-codex/...` model/provider path, not the ordinary OpenAI API-key
provider path.

**A roach-pi slash command appears to do nothing**
The command may depend on Pi's interactive UI context. Ouroboros runs Pi in
one-shot JSON mode, so interactive slash-command UX is not guaranteed. Prefer
normal Seed execution prompts or headless-compatible Pi extensions for
Ouroboros workflows.
