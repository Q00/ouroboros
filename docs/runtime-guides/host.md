# Host-driven execution runtime

The `host` agent runtime delegates execution to the calling MCP host instead
of spawning an external CLI. It is intended for hosts such as DeepSeek Harness
that can spawn their own subagents but do not expose an installable execution
runtime.

## Enable the runtime

```bash
ouroboros setup --runtime host --non-interactive
```

Or set the runtime for the MCP server process:

```bash
export OUROBOROS_AGENT_RUNTIME=host
```

The dsh plugin bundle defaults this variable to `host`. A configured
`orchestrator.runtime_profile.stages.execute` value takes precedence, so check
the runtime profile when execution uses an unexpected backend.

After setup, stay inside the MCP host chat and type:

```text
ooo run
```

The host command calls the job-tracked MCP execution surface. Do not run the
terminal `ouroboros run` command with this runtime; a terminal has no host loop
to receive and submit dispatches.

## Execution flow

```text
ouroboros_start_execute_seed
  -> HostDispatchRuntime parks one execution request
  -> ouroboros_job_wait returns meta.pending_host_dispatches
  -> the MCP host spawns one subagent per actionable dispatch
  -> ouroboros_submit_fanout_results delivers the worker result
  -> the server-side verification gate evaluates the work
  -> ouroboros_job_wait / ouroboros_job_result returns the terminal job state
```

`ouroboros_job_status` exposes pending dispatch identity for observation but
does not claim the actionable announcement. The execution host must use
`ouroboros_job_wait` to receive work.

## Submission contract

Each pending entry provides:

- `dispatch_id` / `fanout_id`
- `session_id`
- `result_correlation_key`
- `subagents[0].prompt`
- `subagents[0].context.working_directory`
- `submit_tool`

Submit a successful worker result with:

```json
{
  "fanout_id": "fanout_...",
  "session_id": "orch_...",
  "correlation_key": "result",
  "results": [
    {
      "key": "result",
      "content": "Worker final output"
    }
  ]
}
```

If no worker was spawned, submit the explicit terminal failure:

```json
{
  "key": "result",
  "undispatched": true
}
```

`null`, empty strings, empty objects, arrays, non-boolean `success` markers,
and success-only objects without substantive output are rejected without
consuming the live dispatch. The host may correct and retry the same
`fanout_id`.

## Lifecycle guarantees

- Every dispatch is scoped to a non-empty session ID.
- Correlation and session mismatches fail closed.
- Duplicate, cancelled, superseded, or completed submissions are stale.
- A live dispatch is announced once. Lost announcements or workers expire at
  the attempt deadline; retry creates a fresh dispatch ID instead of replaying
  work that may still be running.
- Results arriving after the absolute dispatch deadline are not delivered.
- Parked work emits heartbeats so the executor stall detector does not cancel
  a human-paced host prematurely.
- Host failure text is stored as structured metadata and cannot steer runtime
  quota classification.
- The worker prompt names the mandatory task worktree; verification runs in
  that same working directory.
- Hidden verification assertions are not serialized into host payloads.

## Unsupported surfaces

- Terminal `ouroboros run` rejects `host`; use `ooo run` from the MCP host chat.
- Direct `ouroboros_execute_seed` rejects `host`; the host-facing `ooo run`
  command uses the job-tracked `ouroboros_start_execute_seed` surface.
- Evolve and Ralph reject `host` until those jobs carry a discoverable session
  scope end to end.

## Process ownership

The fan-out correlation record is file-backed, while the live waiter belongs
to the serving MCP process. Host-backed jobs therefore stay in that process
instead of moving into detached workers. Mixed-stage `ooo auto` resolves the
actual execute-stage runtime before deciding whether detachment is safe.

## Manual dsh smoke test

Start dsh with the plugin enabled, then run:

```bash
node scripts/live-dsh-interview.cjs \
  "ooo interview deepseek 하네스를 이용해서 마케팅 에이전트를 만들고싶어"
```

Environment overrides:

- `DSH_URL`: dsh web URL, default `http://127.0.0.1:3081`
- `PLAYWRIGHT_MODULE`: module name or absolute Playwright package path
- `LIVE_INTERVIEW_OUT`: output directory, default a temporary directory

The script records the final page text and a full-page screenshot, then prints
whether the Ouroboros interview tool and ambiguity score were observed.
