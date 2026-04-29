---
name: rlm
description: "Run the Dual-layer Recursive Language Model MVP"
aliases: [recursive-language-model]
---

# ooo rlm

Run the isolated Dual-layer Recursive Language Model MVP path.

## Usage

```
ooo rlm [target]
/ouroboros:rlm [target]
```

## Instructions

When the user invokes this skill:

1. Treat the remaining text as raw `ouroboros rlm` CLI arguments. If no
   arguments are supplied, use `src` (implemented by
   `src/ouroboros/cli/commands/rlm.py:53-60` and
   `src/ouroboros/rlm/benchmark.py:80-101`).
2. Run the terminal command through Bash, preserving flags such as
   `--recursive-fixture`, `--cwd`, `--benchmark`, and `--dry-run`:
   ```
   uv run ouroboros rlm <arguments>
   ```
   For a natural-language target with spaces, quote only that target argument:
   ```
   uv run ouroboros rlm "analyze this target"
   ```
3. If the user asks for the checked-in recursive fixture, run:
   ```
   uv run ouroboros rlm --recursive-fixture tests/fixtures/rlm/long_context_truncation.json
   ```
4. If the user asks for a validation-only run, pass `--dry-run`.
5. Do not route this request through `ooo run`, `ooo evolve`, `ouroboros run`, or any evolve command. The RLM MVP is exposed only through this command path (implemented by `src/ouroboros/cli/main.py:17-55`, `src/ouroboros/cli/commands/rlm.py:112-138`, and verified by `tests/unit/cli/test_main.py:101-114`).
