# Interview Latency Benchmark

This guide produces comparable, privacy-safe evidence for interactive
interview latency. It is an observability procedure, not a CI performance gate
and not evidence that every user sees the same wall clock.

## Safety boundary

- Run manually on a maintainer-controlled machine with Codex OAuth auth only.
- Never run this benchmark in CI or with a committed API key.
- Do not attach raw EventStore databases, prompts, answers, errors, logs,
  usernames, hostnames, IP addresses, paths, credentials, or environment values.
- Use the redacted exporter in this repository. Review its JSONL output before
  attaching it to an issue.
- Live runs may contact the configured model provider and consume quota.

## What is measured

The timing-bearing events are documented in `docs/events.md`.

| Phase | Boundary |
|-------|----------|
| `total` | Server handler entry through terminal event creation |
| `ambiguity_scoring` | Awaiting live ambiguity scoring |
| `question_generation` | Awaiting `ask_next_question` |
| `advisory_build` | Preparing advisory request metadata in the server |

The measurements do not separately identify subprocess startup, MCP startup,
network latency, or model execution inside `question_generation`. They also do
not include host-side advisory execution after the MCP response returns.
Measure client-observed wall time separately if that distinction is needed,
and label it as client wall time rather than an EventStore phase.

## Fixed fixture

Use the same synthetic initial context for every run:

```text
Build a local CLI that reads a directory of Markdown files and writes a
deterministic JSON summary. The primary user is a solo maintainer. It must run
on macOS and Linux, perform no network access, preserve source files, process
1,000 small files in under two seconds on the benchmark machine, and include
unit tests. Success means stable key ordering, a non-zero exit on invalid input,
and identical output for identical inputs.
```

Use these answers in order, regardless of wording differences in generated
questions:

1. `The primary user is a solo maintainer running the CLI locally.`
2. `Inputs are UTF-8 Markdown files; output is one deterministic JSON document.`
3. `No network calls or source-file mutation are allowed; macOS and Linux are required.`
4. `Success is identical output for identical inputs, tested error exits, and under two seconds for 1,000 small files on this machine.`

The third and later recorded answers exercise the live ambiguity-scoring path.
If the interview completes before all four answers, record the completion event
and do not invent an additional turn.

## Environment manifest

Record only the following non-secret fields beside each result set:

- UTC benchmark timestamp.
- Ouroboros git commit and package version.
- Python version.
- OS family, OS version, and CPU architecture, without hostname.
- Codex CLI version.
- Configured interview backend, model identifier, and reasoning effort.
- Auth mode as the literal value `oauth`; never record account identity.
- Condition: `cold` or `warm`.
- Measured sample count and fixture revision/hash.
- Coarse concurrent-load note such as `idle` or `normal development load`.

Do not dump process environments. Record individual approved fields manually.

## Cold and warm protocol

Collect at least 20 complete interview samples per condition. Each sample uses
a new interview id and the fixed fixture above.

### Cold

1. Start a new Ouroboros server process for each sample.
2. Do not run an unmeasured interview request in that process.
3. Run the fixed start plus answer sequence until completion or all four answers.
4. Stop the server process after exporting the sample.

Cold does not mean deleting OAuth credentials, keychain entries, package
caches, or operating-system caches. Do not mutate those to manufacture a
stronger cold-start claim.

### Warm

1. Start one Ouroboros server process.
2. Run one unmeasured priming interview with the same fixture.
3. Keep the process alive and collect at least 20 new-interview samples.
4. Do not mix samples collected after configuration, model, or network changes.

## Export

For each measured interview, export only allowlisted timing evidence:

```bash
python scripts/export_interview_latency.py \
  --interview-id interview_0123456789abcdef \
  > interview-latency.jsonl
```

The database defaults to `~/.ouroboros/ouroboros.db`; use `--db` only when the
EventStore lives elsewhere. The exporter opens SQLite in read-only mode and
emits only:

- SHA-256-hashed interview id.
- Event timestamp and event type.
- Allowlisted phase metadata.
- `timings_ms`.

It never emits stored failure text, prompt/answer previews, paths,
credentials, or environment values.

## Summary statistics

Compute statistics independently for each condition, event type, and phase.
Do not combine cold and warm samples or compare results produced by different
commits/configurations as if they were one population.

For each cell report `n`, p50, and p95. With sorted values and sample count
`n`, use nearest-rank percentiles: rank `ceil(0.50 * n)` for p50 and
`ceil(0.95 * n)` for p95. With 20 samples, p95 is the 19th sorted value.
Exclude `null` phases from that phase's sample count and report the reduced `n`.

Recommended report table:

| Condition | Event | Phase | n | p50 ms | p95 ms |
|-----------|-------|-------|---|--------|--------|
| cold | `interview.response.emitted` | `question_generation` | 20+ | | |
| cold | `interview.response.emitted` | `ambiguity_scoring` | 20+ | | |
| warm | `interview.response.emitted` | `question_generation` | 20+ | | |
| warm | `interview.response.emitted` | `ambiguity_scoring` | 20+ | | |

An optional server remainder can be calculated as `total` minus the sum of
non-null measured phases. Label it `unattributed_server_time`; it includes
persistence, metadata preparation outside `advisory_build`, event creation,
and other handler work. It is not host-side time.

## Issue attachment checklist

Before attaching evidence to an issue:

1. Confirm every interview id is hashed.
2. Search the artifact for prompt/answer text, `HOME`, common key prefixes,
   usernames, and absolute path separators.
3. Include the safe environment manifest and exact git commit.
4. State whether the result is cold or warm and give `n`, p50, and p95.
5. State the interpretation boundary: server phase timings do not prove total
   user-visible latency or isolate every operation inside question generation.
