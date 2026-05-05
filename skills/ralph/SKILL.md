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

Ralph is owned by the `ouroboros_ralph` MCP tool. The tool starts one
background Ralph job, runs repeated `evolve_step` generations inside that job,
and stops only when QA passes, convergence is reached, a terminal evolution
action occurs, cancellation is requested, or `max_generations` is reached.

The client skill should not reimplement the loop. It should start the MCP job
and monitor it with the normal job tools.

## Instructions

When the user invokes this skill:

1. **Parse the request**: Extract what needs to be done and prepare the seed or
   lineage input expected by `ouroboros_ralph`.

2. **Start Ralph** by calling `ouroboros_ralph` with:
   - `lineage_id`: existing lineage id, or a generated stable id for a new loop
   - `seed_content`: Seed YAML for generation 1 when starting a new lineage
   - `execute`: default `true`
   - `parallel`: default `true`
   - `skip_qa`: default `false`
   - `project_dir`: explicit target project directory when known
   - `max_generations`: default `10` unless the user requests a tighter bound

3. **Report the returned job id** concisely:

   ```
   [Ralph] Started background loop: <job_id>
   Lineage: <lineage_id>
   ```

4. **Monitor progress** with job tooling:
   - `ouroboros_job_wait(job_id, cursor, timeout_seconds=120)` for long polling
   - `ouroboros_job_status(job_id)` for a quick status check
   - `ouroboros_job_result(job_id)` when the job is terminal
   - `ouroboros_cancel_job(job_id)` if the user says stop/cancel

5. **On termination**, summarize the final job result and next step:
   - Success / convergence: `Next: ooo evaluate for formal 3-stage verification`
   - Max generations / failure: summarize the stop reason and suggest
     `ooo unstuck`, `ooo interview`, or a narrower Ralph retry
   - Cancelled: confirm cancellation and preserve the job id for later inspection

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
