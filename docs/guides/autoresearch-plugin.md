# Autoresearch Plugin Guide

The `autoresearch` plugin is the bridge from a Karpathy-style research loop to
`ouroboros auto`.

It does **not** run the experiment by itself. The plugin prepares a bounded,
auditable handoff from an experiment repository into Ouroboros. After that,
`ouroboros auto` owns the normal auto pipeline:

```text
plugin prepare
  -> generated Seed + auto goal
  -> ouroboros auto
  -> interview
  -> seed generation / review / repair
  -> run execution
  -> evaluate / reflect-or-recover
```

In the code, the coarse auto phases are `INTERVIEW`, `SEED_GENERATION`,
`REVIEW`, `REPAIR`, `RUN`, `RALPH_HANDOFF`, `EVALUATE`, and recovery/terminal
states. The runtime routing layer also exposes `interview`, `execute`,
`evaluate`, and `reflect` bindings, so an autoresearch run can use different
agent backends for those parts of the loop.

## Install

Install from the reference plugin catalog:

```bash
ouroboros plugin add https://github.com/Q00/ouroboros-plugins --plugin autoresearch
```

`autoresearch` needs two required scopes:

- `filesystem:read`: inspect `program.md`, `prepare.py`, `train.py`
- `filesystem:write`: write `.ouroboros/autoresearch/*` handoff artifacts

On current Ouroboros versions, `plugin add` prompts to grant these required
non-destructive scopes. If you decline or installed in a non-interactive
context, grant them explicitly:

```bash
ouroboros plugin trust autoresearch \
  --scope filesystem:read \
  --scope filesystem:write
```

For local plugin development, install from a local `Q00/ouroboros-plugins`
checkout instead:

```bash
cd /path/to/ouroboros-plugins
ouroboros plugin add . --plugin autoresearch
```

## Expected Experiment Layout

The target repository should contain:

```text
program.md   # research brief, constraints, metric, stop condition
prepare.py   # fixed data prep / evaluation helpers
train.py     # editable experiment code
```

By default, the plugin assumes:

- target file: `train.py`
- support file: `prepare.py`
- metric: `val_bpb`
- verification command: `uv run train.py`
- edit boundary: only `train.py`

You can override paths and command:

```bash
ouroboros auto-research prepare /path/to/research-repo \
  --goal "Improve validation bits-per-byte while preserving reproducibility" \
  --program-file program.md \
  --target-file train.py \
  --support-file prepare.py \
  --metric val_bpb \
  --train-command "python3 train.py"
```

All layout paths must stay inside the target repository. Absolute paths and
`..` escapes are rejected.

## Prepare The Handoff

First inspect readiness:

```bash
ouroboros auto-research inspect /path/to/research-repo
```

Then prepare:

```bash
ouroboros auto-research prepare /path/to/research-repo \
  --goal "Improve val_bpb with a bounded, reproducible experiment log" \
  --max-experiments 8 \
  --experiment-seconds 300 \
  --train-command "python3 train.py"
```

The plugin writes:

```text
.ouroboros/autoresearch/seed.md
.ouroboros/autoresearch/auto_goal.txt
.ouroboros/autoresearch/handoff.json
```

`handoff.json` contains the recommended `ouroboros auto` command, metric,
experiment budget, editable file boundary, and verification command.

The prepared Seed also includes the details the auto QA gate expects before
execution: experiment 1 as an unmodified baseline, experiments 2-N as concrete
candidate changes inside the target file, explicit non-goals, runtime context,
metric parsing rules, and the verification command. If those details are too
vague, `ouroboros auto` can stop at Seed QA instead of running an
under-specified research loop.

## Run With Ouroboros Auto

Start the full auto loop from the generated goal:

```bash
ouroboros auto "$(cat /path/to/research-repo/.ouroboros/autoresearch/auto_goal.txt)"
```

Use `--skip-run` when you only want Ouroboros to converge the Seed and stop
before editing/running experiments:

```bash
ouroboros auto "$(cat /path/to/research-repo/.ouroboros/autoresearch/auto_goal.txt)" --skip-run
```

Use `--complete-product` when you want the post-run Ralph/evaluation loop to
continue toward a finished product within the configured budgets:

```bash
ouroboros auto "$(cat /path/to/research-repo/.ouroboros/autoresearch/auto_goal.txt)" --complete-product
```

## What To Expect

During a healthy autoresearch run, the plugin-prepared Seed should make
Ouroboros:

1. Interview the research objective until the metric, edit boundary, budget, and
   stop condition are explicit.
2. Generate and review a Seed before execution.
3. Edit `train.py` only, unless the ledger explicitly widens scope.
4. Run the configured verification command for each experiment.
5. Evaluate the output against the Seed acceptance criteria.
6. Reflect or recover when the evaluation fails, until the run completes,
   blocks, or exhausts budget.

The plugin's job is to make that loop auditable. It preserves the research
brief, target file, metric, budget, and verification command in durable
handoff artifacts instead of teaching Ouroboros core any neural-network-specific
workflow.
