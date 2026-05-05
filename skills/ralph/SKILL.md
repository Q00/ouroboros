---
name: ralph
description: "MCP-owned Ralph loop around background evolve_step jobs"
---

# /ouroboros:ralph

MCP-owned Ralph loop around background `evolve_step` jobs. "The boulder never stops."

## Usage

```
ooo ralph "<your request>"
/ouroboros:ralph "<your request>"
```

**Trigger keywords:** "ralph", "don't stop", "must complete", "until it works", "keep going"

## How It Works

Ralph is owned by the `ouroboros_ralph` MCP tool. In non-plugin runtimes, the
tool starts one background Ralph job, runs repeated `evolve_step` generations
inside that job, and stops only when QA passes, convergence is reached, a
terminal evolution action occurs, cancellation is requested, or
`max_generations` is reached. In OpenCode plugin mode, the MCP tool returns a
`delegated_to_plugin` envelope with `job_id=None`; the bridge plugin dispatches
a child Task session that owns the loop instead of creating a local JobManager
job.

The client skill should not reimplement the loop. It should start the MCP-owned
Ralph surface once, then follow either the returned job tools path or the
OpenCode Task widget path.

## Instructions

When the user invokes this skill:

### Load MCP Tools (Required first)

The Ouroboros MCP tools are often registered as deferred tools that must be
explicitly loaded before use. Do this before preparing input or calling Ralph:

1. Use the `ToolSearch` tool to find and load the Ralph/job MCP tools:
   ```
   ToolSearch query: "+ouroboros ralph job"
   ```
2. The loaded tools may be exposed under plugin-prefixed names such as
   `mcp__plugin_ouroboros_ouroboros__ouroboros_ralph`. Use the actual tool
   names returned by `ToolSearch`; the bare names below are the canonical MCP
   tool names for documentation.
3. Confirm that `ouroboros_ralph` and the job tools (`ouroboros_job_wait`,
   `ouroboros_job_status`, `ouroboros_job_result`, and
   `ouroboros_cancel_job`) are callable. If the tools are unavailable, stop and
   tell the user that Ralph requires the Ouroboros MCP runtime.

### Ralph Flow

1. **Prepare lineage input**:
   - If the user provides an existing `lineage_id` and explicitly wants to
     continue it, reuse that `lineage_id` and omit `seed_content` unless they
     explicitly provide an updated Seed.
   - If the user provides Seed YAML for a new Ralph run, use it as
     `seed_content` and generate a fresh `lineage_id` for this run. Keep
     `lineage_id` separate from Seed, interview, and session IDs so separate
     Ralph runs over the same Seed do not collide.
   - If the user provides only a plain request (`ooo ralph "<request>"`), do
     not freehand Seed YAML and do not pass raw natural-language text as
     `seed_content`. Route through the authoritative Seed path first:
     `ooo interview` to capture requirements, then `ooo seed` /
     `ouroboros_generate_seed` to produce validated Seed YAML with the normal
     ambiguity gate. Use that generated Seed YAML as `seed_content`. If an
     interview/seed session already exists in context, reuse that validated Seed
     output instead of regenerating it.

2. **Start Ralph** by calling `ouroboros_ralph` with:
   - `lineage_id`: existing lineage id for an explicit continuation, otherwise a
     freshly generated stable id for this Ralph run, such as
     `ralph-<short-slug>-<uuid>`; do not use a Seed/interview id by itself
   - `seed_content`: valid Seed YAML for generation 1 when starting a new lineage
   - `execute`: default `true`
   - `parallel`: default `true`
   - `skip_qa`: default `false`
   - `project_dir`: explicit target project directory when known
   - `max_generations`: default `10` unless the user requests a tighter bound

3. **Handle the start response**:
   - If `response.meta.job_id` is present, report it concisely and retain the
     job cursor from `response.meta.cursor`:

     ```
     [Ralph] Started background loop: <job_id>
     Lineage: <lineage_id>
     ```

   - If `response.meta.status == "delegated_to_plugin"` and
     `response.meta.job_id is None`, report that OpenCode plugin mode delegated
     the loop to a child Task session. Do not call `ouroboros_job_wait`,
     `ouroboros_job_result`, or `ouroboros_cancel_job` without a job id; follow
     the host Task widget/session lifecycle instead.

4. **Monitor non-plugin job progress** with job tooling when a `job_id` exists:
   - `ouroboros_job_wait(job_id, cursor, timeout_seconds=120)` for long polling;
     after every wait/status response, update `cursor = response.meta.cursor`
   - `ouroboros_job_status(job_id)` for a quick status check
   - `ouroboros_job_result(job_id)` when the job is terminal
   - `ouroboros_cancel_job(job_id)` if the user says stop/cancel

5. **On non-plugin job termination**, fetch `ouroboros_job_result(job_id)` and
   summarize the final job result and next step:
   - Success / convergence: summarize the final generation output, QA verdict,
     and any `worktree_path` / `worktree_branch` returned in job metadata. Do not
     present `ooo evaluate` as an automatic next step for Ralph results: the
     Ralph job contract preserves the evolution `lineage_id`, but it does not
     reliably preserve a separate execution `session_id` for the evaluate
     workflow. If a valid execution `session_id` is explicitly available from a
     separate run result, keep it distinct from the Ralph `lineage_id` and follow
     the `ooo evaluate <session_id>` contract; otherwise state that formal
     evaluation needs a real execution session and should not be invoked from the
     Ralph lineage id alone.
   - Max generations / failure: summarize the stop reason and suggest
     `ooo unstuck`, `ooo interview`, or a narrower Ralph retry
   - Cancelled: confirm cancellation and preserve the job id for later inspection

6. **On OpenCode plugin delegation**, rely on the child Task result as the
   terminal surface. Summarize the Task completion/error state and lineage id; do
   not claim a local Ralph job can be polled or cancelled.

## Tool Mapping

| Skill action | MCP tool |
| --- | --- |
| Start Ralph loop | `ouroboros_ralph` |
| Wait for progress | `ouroboros_job_wait` |
| Fetch final result | `ouroboros_job_result` |
| Cancel loop | `ouroboros_cancel_job` |
| Inspect current status | `ouroboros_job_status` |

## The Boulder Never Stops

This is the key phrase. Ralph does not give up:

- Each failure is data for the next attempt.
- Verification drives the loop.
- Only success, convergence, terminal failure, cancellation, or max-generation
  limits stop it.
