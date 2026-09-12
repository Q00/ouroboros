# GitHub Copilot ACP Runtime (Experimental)

Use Copilot's Agent Client Protocol to see activity **while a task is running**,
instead of waiting for the final response from `copilot -p`.

> GitHub describes ACP as **public preview**. Ouroboros keeps it isolated behind
> the existing `copilot` backend. The [CLI transport](copilot.md) remains the
> default and is not removed. This implementation uses **stdio only**.

## Enable ACP

Prerequisites are the same as [Copilot CLI](copilot.md#prerequisites), plus a
Copilot binary that supports `copilot --acp --stdio`, protocol version 1,
`session/new`, and `session/prompt`. Copilot CLI 1.0.83 was smoke-tested on Linux;
feature compatibility is checked by the handshake, not a guessed minimum
version number.

Authenticate Copilot with your organization-approved account before starting
Ouroboros. No new API key, TCP listener, display, browser session during task
execution, Node package, or ACP SDK dependency is required.

```bash
ouroboros setup --runtime copilot
ouroboros config set orchestrator.copilot_transport acp
```

Equivalent `~/.ouroboros/config.yaml`:

```yaml
orchestrator:
  runtime_backend: copilot
  copilot_transport: acp       # cli (default) | acp
  copilot_acp_fallback: true   # guarded fallback before prompt submission only
llm:
  backend: copilot
```

For a process-local override:

```bash
OUROBOROS_AGENT_RUNTIME=copilot \
OUROBOROS_COPILOT_TRANSPORT=acp \
ouroboros run workflow seed.yaml
```

The backend name is still **`copilot`**, not `copilot-acp`. Existing CLI path,
model discovery, role/profile selection, custom instructions, and setup/MCP
registration are reused. `CopilotCliLLMAdapter` still handles LLM-only calls
(interview, QA, etc.) using `-p`; ACP changes the **agent execution transport**.
Restart an already running Ouroboros MCP server after changing transport.

| Setting | Default | Meaning |
|---------|---------|---------|
| `orchestrator.copilot_transport` / `OUROBOROS_COPILOT_TRANSPORT` | `cli` | Choose `cli` or `acp` |
| `orchestrator.copilot_acp_fallback` / `OUROBOROS_COPILOT_ACP_FALLBACK` | `true` | Permit the limited fallback described below |
| `orchestrator.copilot_cli_path` / `OUROBOROS_COPILOT_CLI_PATH` | PATH lookup | Same executable for both transports |

Trusted environment overrides take precedence over YAML. Invalid non-empty
transport/boolean overrides fail explicitly. Untrusted project `.env` files
cannot change transport or fallback policy.

## What streams

Copilot ACP emits JSON-RPC `session/update` notifications. These are **not** the
Copilot SDK's `assistant.message_delta` / `tool.execution_start` wire events.
The translator uses the actual ACP `params.update.sessionUpdate` discriminator:

| ACP input | Normalized `AgentMessage` | `runtime_event_type` |
|-----------|---------------------------|----------------------|
| `agent_message_chunk` with text | assistant, exact `content_delta` | `assistant.message_delta` |
| `tool_call` | assistant tool envelope | `tool.started` |
| `tool_call_update`, not terminal | system, `subtype=tool_progress` | `tool.progress` |
| `tool_call_update`, `completed` or `failed` | tool, `subtype=tool_result` | `tool.result` |
| `plan` | system status | `agent.plan` |
| `session/request_permission` and client decision | system audit | `acp.permission_resolved` |
| `session/prompt` response with `stopReason=end_turn` | final result | `turn.completed` |
| Other stop reasons / protocol failures | error result | `turn.failed` |

Actual observed payloads, with session/tool IDs shortened:

```json
{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"…","update":{"sessionUpdate":"tool_call","toolCallId":"toolu_…","title":"Run two-stage python print command","kind":"execute","status":"pending","rawInput":{"command":"python3 -c \"…\""}}}}
{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"…","update":{"sessionUpdate":"tool_call_update","toolCallId":"toolu_…","content":[{"type":"content","content":{"type":"text","text":"ACP_STAGE_1\n"}}]}}}
{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"…","update":{"sessionUpdate":"tool_call_update","toolCallId":"toolu_…","content":[{"type":"content","content":{"type":"text","text":"ACP_STAGE_1\nACP_STAGE_2\n"}}]}}}
{"jsonrpc":"2.0","id":3,"result":{"stopReason":"end_turn","usage":{"inputTokens":12502,"outputTokens":196,"totalTokens":12698,"thoughtTokens":0,"cachedReadTokens":6163,"cachedWriteTokens":6335}}}
```

The two shell progress snapshots arrived four seconds apart, before the prompt
response. Shell progress is **cumulative output**, so it is stored as
`tool_output`, not falsely labeled as disjoint output deltas.

`status=completed` means that the **tool finished**, not that its shell command
succeeded. Observed terminal output includes structured command status:

```json
{"rawOutput":{"content":"…\n<shellId: 0 completed with exit code 1>","contents":[{"type":"shell_exit","shellId":"0","exitCode":1,"outputTruncated":false,"cwd":"…","outputPreview":"…"}]}}
```

Ouroboros preserves this as `exit_code=1` and an error tool result. A trailing
Copilot shell-exit footer is a compatibility fallback when structured exit
status is absent. Test failures are therefore visible even when the agent later
fixes them and the overall turn succeeds.

Other observed updates are `agent_thought_chunk`, `usage_update`,
`config_option_update`, and `available_commands_update`. Private thought chunks
are deliberately **not** exposed or persisted as task activity. Configuration
and command discovery are not task text. `usage_update.used/size` describes the
context window, not billable tokens; only validated prompt-result counters are
mapped to the existing usage fields.

## Architecture and persistence

```text
Herdr / SSH / Coder VM (optional environment management)
  → Ouroboros AgentRuntime
    → CopilotAcpRuntime
      → CopilotAcpClient → copilot --acp --stdio
      → CopilotAcpEventTranslator → AgentMessage
    → existing runtime projection + ExecutionEventEmitter
    → existing EventStore → live consumers / history / replay
```

There is no UI-specific event path and no second database schema. Both the
sequential runner and parallel executor persist each assistant delta and each
tool/progress/result event through their normal progress emitter. Whitespace,
tool IDs, transport metadata, and available parent/agent correlation survive
projection. Tool progress is not counted as an extra tool invocation.

Each invocation owns one process, one native session, and one prompt. ACP sets
the workspace through `session/new.cwd`, with no client filesystem/terminal
capabilities and no additional MCP servers supplied by this transport.
Existing `ooo` skill interception remains in Ouroboros.

Frames are bounded to 8 MiB; tool output previews to 64 KiB; final-answer
assembly to 2 MiB (with `response_truncated` metadata). Oversized assistant
chunks are split into bounded deltas without losing their replay text. The
client drains stderr independently and bounds startup/idle waits. Runtime
constructor timeout overrides are available; defaults are 60 seconds for
startup and 300 seconds of stdout inactivity during a prompt. This is not a
five-minute total task limit.

Cancellation, iterator closure, and live-handle termination send
`session/cancel` when work is outstanding, then close stdin and reap the owned
child/process group if graceful shutdown fails. No prompt is replayed after an
uncertain submission.

## Permissions and authentication

- The native `--available-tools=...` filter enforces the task's tool envelope,
  including **`tools=[]`**. Ouroboros names are translated, for example
  `Read → view`, `Glob → glob`, `Grep → grep`, and `Bash → bash` plus shell
  session-control tools. Copilot's native search tool is `grep`, not `rg`.
  **Measured preview quirk:** CLI 1.0.83 interprets `--available-tools=` as
  unrestricted, and an overlapping exclusion did not remove an explicitly
  allowed tool. ACP therefore enforces no-tools with a nonmatching,
  per-invocation nonce allow-list entry instead. This was verified to disable
  native tools while retaining text-only answers. Its initial disabled/unknown
  tool notices are normalized as status, not included in the final answer.
  This workaround is confined to ACP and its guarded fallback; the legacy
  CLI transport is unchanged.
- `default` exposes no tools, matching the existing Copilot policy.
  `acceptEdits` permits only requested, understood local operations.
  An omitted envelope in that mode defaults to Read/Glob/Grep/Edit/Write/Bash.
- Runner-requested `bypassPermissions` is deliberately **capped to
  `acceptEdits`**. Parameter negotiation reports this as `translated`.
  ACP never adds `--yolo`, `--allow-all`, or `--allow-all-tools`.
- Server permission requests are answered only with **`allow_once`** or
  cancellation/denial, never a persistent approval. Unknown permission kinds,
  foreign sessions, and requested paths outside the canonical workspace
  (including symlink escapes) are denied. Writes need an identifiable target.
- This is a tool/approval policy, **not an OS sandbox**. In particular,
  explicitly permitting Bash lets shell commands run as your local user.
  `--add-dir` is not a general filesystem or network isolation mechanism.
  Use an appropriately isolated workspace for untrusted tasks.

Ouroboros preserves Copilot's existing authentication environment; it does not
copy tokens into YAML or silently change accounts. A stale `GITHUB_TOKEN` can
override a working CLI login. If that is your diagnosed problem, omit only that
known-invalid variable from the child command, or use the raw probe's explicit
`--ignore-github-token` switch. Do not treat an ACP handshake or a `gh` login to
an unrelated GitHub Enterprise host as proof of Copilot authorization: test a
session and prompt.

Copilot Business availability remains subject to your organization's Copilot
CLI policies. Authentication/authorization failures are surfaced and **never
trigger automatic fallback**. Ouroboros does not modify organization policy.

## Fallback and limitations

The legacy `cli` transport remains the default. With ACP explicitly enabled and
fallback allowed, unsupported flags, incompatible protocol versions, malformed
startup replies, or session creation failures can select legacy execution
**only before the prompt is sent** and **only for a tool-less or read-only
Read/Glob/Grep envelope**. A `runtime.fallback` event explains the switch.

Mutating envelopes fail rather than lose ACP permission callbacks. Auth errors,
executable-attestation failures, cancellation, and failures after prompt
submission never replay through another transport. Disable fallback for smoke
tests or when streaming is mandatory:

```bash
ouroboros config set orchestrator.copilot_acp_fallback false
```

To deliberately return to the original runtime:

```bash
ouroboros config set orchestrator.copilot_transport cli
```

Remaining boundaries:

- **No native session resume/load** in this implementation, even when Copilot
  advertises that capability. Native session IDs are recorded as diagnostic
  `acp_session_id` metadata, not resumable handle selectors;
  `targeted_resume=False` and returned handles have `can_resume=False`.
  A later Ouroboros dispatch can retain scope/profile metadata but starts a new
  native session; it does not recover Copilot conversation state.
  Ouroboros history/replay still works.
- Optional `agentId`, `parentToolCallId`, `parentId`, source IDs/timestamps, and
  MCP correlation fields are retained when present. Tool correlation does not
  assume sequential execution. A live foreground `explore` probe emitted a
  Task call (`kind=other`, `rawInput.agent_type=explore`) and three inner
  Read/Glob calls before completion, but **no agent or parent IDs** on those
  updates. Their individual tool IDs are retained; ancestry is not guessed
  from timing or ID prefixes. Parent/child text attribution and parallel
  subagent visualization remain incomplete when Copilot omits correlation.
- `structured_output=True` means structured **runtime events**, not native
  JSON-schema-constrained model responses.
- No changes to Herdr, Coder, desktop/web/TUI layouts, or remote session
  persistence are included. The smoke tests were headless Linux runs, not a
  Herdr/SSH end-to-end deployment test.

## Reproduce the probes

From a checkout with its existing development dependencies installed:

Use the checkout's environment. An existing setup-owned MCP launcher may still
run the published PyPI package rather than these local changes; see
[the development loop](../contributing/developing.md) before testing through a
host such as Copilot or Herdr.

```bash
# Raw wire protocol; read-only by default, independent of Ouroboros imports.
python3 scripts/probe-copilot-acp.py \
  --workspace /path/to/safe-repo \
  --log .acp-artifacts/probe.ndjson

# Actual runtime → projection → EventStore, with live timestamps and no fallback.
.venv/bin/python scripts/smoke-copilot-acp.py \
  --workspace /path/to/safe-repo \
  --output-dir .acp-artifacts/runtime-smoke
```

Select a model available to your subscription with `--model`. For a raw shell
progress probe, pass `--tools bash --allow-command '<exact command>'` and ask for
that exact command in `--prompt`; all other execution requests remain denied.
Run each runtime smoke with a fresh output directory. It writes
`messages.ndjson`, `events.db`, and `summary.json`.

For a long-running acceptance test, use a **disposable local fixture**, not an
unreviewed change to your working repository. Seed an intentionally failing
test, then explicitly allow `--tools Read,Edit,Bash` and ask the agent to run,
inspect, fix only the fixture implementation, and rerun. Preserve useful delays
in the tests. The final scoped three-test fixture streamed its first tool at
7.333 seconds, initial failures and retest output during execution, and its
final answer at 53.354 seconds; 37 normalized events were persisted.
Independently rerun the fixture's tests afterward.

Raw probe logs can include private thoughts, prompts, and tool output; normalized
runtime logs exclude thoughts but can still contain sensitive workspace data.
Keep both private and out of commits. The scripts do not upload their logs.

Offline protocol, translator, permissions, selection, cancellation, fallback,
and durable-streaming tests require no Copilot account:

```bash
.venv/bin/python -m pytest \
  tests/unit/copilot/test_acp_client.py \
  tests/unit/copilot/test_acp_events.py \
  tests/unit/copilot/test_acp_permissions.py \
  tests/unit/orchestrator/test_copilot_acp_runtime.py \
  tests/unit/config/test_copilot_transport.py \
  tests/integration/test_copilot_acp_runtime.py -q
```

The fake server waits for the consumer to persist a tool start **before** it
finishes its prompt. A close/reopen replay check verifies the same deltas and
tool results survived in SQLite.

## References

- [GitHub Copilot ACP server](https://docs.github.com/en/copilot/reference/copilot-cli-reference/acp-server)
- [About Copilot CLI and its security model](https://docs.github.com/en/copilot/concepts/agents/copilot-cli/about-copilot-cli)
- [Agent Client Protocol](https://agentclientprotocol.com/)
- [Ouroboros configuration reference](../config-reference.md)
- [Runtime capability matrix](../runtime-capability-matrix.md)
