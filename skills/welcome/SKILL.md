---
name: welcome
description: "First-touch experience for new Ouroboros users"
disable-model-invocation: true
---

# /ouroboros:welcome

Interactive onboarding for new Ouroboros users.

## Usage

```
/ouroboros:welcome              # First-time or update onboarding
/ouroboros:welcome --skip       # Skip welcome, mark as shown
/ouroboros:welcome --force      # Force re-run welcome even if shown
```

## Instructions

Ask every question with the active runtime's `ask_user` capability, in the
user's language. The Korean labels below are the canonical wording.

### Python Runtime (Required)

Define this resolver in the same shell before any snippet below. It accepts
only Python 3.12 or newer (`python3`, then `python`, then uv) and preserves
arguments and stdin. Quote every argument passed to `ouroboros_python`.

<!-- ouroboros-python-resolver:start -->
```bash
ouroboros_python() {
  if command -v python3 >/dev/null 2>&1 &&
    (unset PYTHONHOME PYTHONPATH PYTHONPLATLIBDIR PYTHONEXECUTABLE __PYVENV_LAUNCHER__; command python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 12))') >/dev/null 2>&1
  then
    (unset PYTHONHOME PYTHONPATH PYTHONPLATLIBDIR PYTHONEXECUTABLE __PYVENV_LAUNCHER__; command python3 "$@")
    return
  fi
  if command -v python >/dev/null 2>&1 &&
    (unset PYTHONHOME PYTHONPATH PYTHONPLATLIBDIR PYTHONEXECUTABLE __PYVENV_LAUNCHER__; command python -c 'import sys; raise SystemExit(sys.version_info < (3, 12))') >/dev/null 2>&1
  then
    (unset PYTHONHOME PYTHONPATH PYTHONPLATLIBDIR PYTHONEXECUTABLE __PYVENV_LAUNCHER__; command python "$@")
    return
  fi
  if command -v uv >/dev/null 2>&1; then
    (unset PYTHONHOME PYTHONPATH PYTHONPLATLIBDIR PYTHONEXECUTABLE __PYVENV_LAUNCHER__; command uv run --no-project --quiet --python '>=3.12' python "$@")
    return
  fi
  printf '%s\n' 'Ouroboros skills require Python >= 3.12 or uv on PATH.' >&2
  return 127
}
```
<!-- ouroboros-python-resolver:end -->

### Bundled Scripts

This skill's programs are files in `scripts/` next to this `SKILL.md`. Run
them with the commands below; never retype, rewrite or edit them. Resolve
`scripts/` from the directory containing this `SKILL.md`, even if that
directory is hidden, and set `WELCOME_DIR` to that directory's absolute path in
the same shell call as the resolver:

```bash
WELCOME_DIR='<absolute path of the directory containing this SKILL.md>'
```

Every command below runs after the resolver and that line, in one shell call.
Type the arguments exactly as shown: the shell expands `"$HOME"` and the other
variables, and the scripts take those values from their arguments.
If a script is missing, tell the user the installed Ouroboros skill files are
incomplete and that reinstalling or updating Ouroboros restores them; do not
reconstruct the program.

### Pre-Check: Already Completed?

Run the readiness check:

```bash
ouroboros_python - precheck "$HOME" "${CODEX_HOME:-$HOME/.codex}" "${SETUP_READY:-}" "${CODEX_READY:-}" "${LEGACY_CODEX_MODEL_MIGRATION_REQUIRED:-}" < "$WELCOME_DIR/scripts/welcome_state.py" || printf 'completed= setup= codex= migration=\n'
```

It prints one line, `completed=... setup=... codex=... migration=...` (the
fallback is the line it printed when no Python 3.12+ or uv is available).
`completed` is the completion marker `WELCOME_COMPLETED` (legacy
`welcomeShown: true` also counts). `setup`, `codex` and `migration` are `true`
when `SETUP_READY`, `CODEX_READY` and `LEGACY_CODEX_MODEL_MIGRATION_REQUIRED`
hold; the sections below use those names.

Before honoring that completion marker, determine whether setup is ready for
the active runtime.
A previously completed welcome must never hide the setup gate from a user who
chose **나중에** or whose setup was later removed.

Claude Code is ready when `~/.ouroboros/config.yaml` has
`orchestrator.runtime_backend` in `{claude, claude_mcp}` and `llm.backend:
claude`. The marketplace plugin owns its MCP registration, so host-owned
`~/.claude/mcp.json` is never part of readiness.

If `SETUP_READY` is not true, check Codex. It is ready when both backends are
`codex`, the Codex CLI (`OUROBOROS_CODEX_CLI_PATH`, else
`orchestrator.codex_cli_path`, else `codex` on PATH) is runnable, and
`mcp_servers\.ouroboros` in `$CODEX_HOME/config.toml` (any TOML key spelling)
has a runnable `command` or a non-blank `url`.

### Legacy Codex Model Migration

Older configurations saved `gpt-5` into all four stage-model fields. That is
now an explicit pin that stops Codex App/CLI model changes from taking effect.
Never rewrite a possible user pin silently. When Codex is ready, the Pre-Check's
`migration` field detects that exact legacy shape (or an interrupted migration)
once.

If `CODEX_READY` and `LEGACY_CODEX_MODEL_MIGRATION_REQUIRED` are both true, ask
"현재 설정은 모든 단계에서 gpt-5를 고정해 두고 있어요. Codex에서 선택한 모델을 자동으로 사용하도록 바꿀까요?"
(header `모델 설정`) with these options:

- **Codex 선택으로 전환하기 (권장)**: App이나 CLI에서 바꾼 모델을 모든 단계가 자동으로 따라가요.
  Atomically rewrite the four pins:

```bash
ouroboros_python - "$HOME/.ouroboros/config.yaml" < "$WELCOME_DIR/scripts/migrate_codex_models.py"
```

  `default` sends no model pin to Codex; it is not a model named "default".
  Confirm the rewrite succeeded before recording the decision. An interrupted
  rewrite leaves a partial `gpt-5`/`default` state that the next check offers
  again.
- **gpt-5 고정 유지하기**: 지금처럼 모든 단계를 gpt-5로 계속 실행해요. Do not
  change `config.yaml`.

For either choice, merge exactly one `codexModelMigration` marker into
`~/.ouroboros/prefs.json`, keeping existing keys:

```bash
ouroboros_python - "automatic-v1" < "$WELCOME_DIR/scripts/record_codex_model_migration.py"
```

Pass `kept-gpt-5-v1` instead of `automatic-v1` for the keep choice. If welcome
was already completed, confirm briefly and exit; do not also ask the generic
welcome question.

### Routing After the Pre-Check

- **Completed, `SETUP_READY` or `CODEX_READY` true, no `--force`**: ask
  "Ouroboros welcome was already completed on $WELCOME_COMPLETED. What would
  you like to do?" with **Skip** (continue to work, recommended; exit) and
  **Re-run welcome** (continue to Step 1).
- **Completed but neither ready**: skip that prompt and go to the Setup Gate.
- **`--skip`**: write the completion prefs (Step 4), print
  `Ouroboros welcome skipped. Run /ouroboros:welcome --force to re-run onboarding.`,
  and exit.

### Setup Gate: First Use

Use the readiness the Pre-Check printed; a global `config.yaml` alone is not
enough unless it names a runtime these gates recognize. To recheck after a
setup run (or when the Pre-Check did not run), use these standalone gates,
which print their verdict. Claude Code:

```bash
ouroboros_python - setup-gate "$HOME" < "$WELCOME_DIR/scripts/welcome_state.py" || echo SETUP_REQUIRED
```

If that printed `SETUP_READY`, skip the Codex gate and continue to Step 1.
Otherwise check Codex:

```bash
ouroboros_python - codex-gate "$HOME" "${CODEX_HOME:-$HOME/.codex}" < "$WELCOME_DIR/scripts/welcome_state.py" || echo CODEX_SETUP_REQUIRED
```

If neither is ready (`SETUP_REQUIRED` and `CODEX_SETUP_REQUIRED`, including a
config that names a runtime neither gate recognizes), ask
"Ouroboros를 처음 사용하시네요. 시작하기 전에 실행 환경을 설정할까요?"
(header `Ouroboros 시작하기`):

- **설정하고 시작하기 (권장)**: 한 번만 설정하면 바로 사용할 수 있어요. Run the
  setup for the active host yourself; never ask the user to copy a command the
  host can run. In Codex App or Codex CLI, run `ouroboros setup --runtime codex`
  when the executable is installed. For a Marketplace-plugin-only install run
  `uvx --isolated --python '>=3.12' --from 'ouroboros-ai[mcp]' ouroboros setup --runtime codex`
  instead. In Claude Code, follow `../setup/SKILL.md`.
- **나중에**: 지금은 기본 안내만 보고 나중에 설정할게요. Continue the welcome
  flow without claiming MCP-backed features are ready.

After a successful **Codex** setup, ask "설정이 완료됐어요. 기본적으로 Codex에서
선택한 모델을 사용합니다. 모델은 언제든 나중에 바꿀 수 있어요." (header `준비 완료`):

- **바로 시작하기 (권장)**: 기본 모델로 바로 작업을 시작해요. Continue to Step 1.
- **직접 모델 설정하기**: 단계별로 모델을 바꾸거나 목록에 없는 모델 ID를 입력해
  고정해요. Read and follow `../config/SKILL.md`. On the user's local Codex App
  or CLI it opens the settings UI in their browser at a
  temporary `localhost` address (not an external website). It offers **Use Codex default model** and
  **Enter another model ID…** for a deliberate stage pin. Continue to Step 1
  when the settings session ends.

In Claude Code, `../setup/SKILL.md` asks the equivalent model question itself;
do not ask it again. Do not show this gate again once a runtime is ready; the
settings UI stays available through `ooo config`.

### Step 1: Banner

```
Welcome to Ouroboros!

Most AI coding fails at the input, not the output.
Ouroboros exposes hidden assumptions BEFORE any code is written.

Interview -> Seed -> Execute -> Evaluate
    ^                            |
    +---- Evolutionary Loop -----+
```

Ordinary Claude setup uses the default `[claude]` Agent SDK profile on MCP 1.x.
Never inspect or mutate `~/.claude/mcp.json` as a health check. If Ouroboros
MCP tools are not callable here, say that MCP-backed commands need a supported
host setup, which owns an isolated `[mcp]` launcher; `[claude-sdk]` and
`[mcp]` are never installed together.

### Step 2: Persona

Ask "What brings you to Ouroboros?" with **New project idea** (crystallize a
vague idea into a Seed), **Tired of rewriting prompts** (AI keeps building the
wrong thing), and **Just exploring**. Reply in 1-2 personalized sentences.

### Step 3: First Action

Show the core commands: `ooo interview` (Socratic Q&A that exposes hidden
assumptions), `ooo seed` (freeze answers into a Seed), `ooo run` (execute a
Seed), `ooo evaluate` (verify against the acceptance criteria), `ooo unstuck`
(lateral thinking), `ooo config` (settings), `ooo help` (full reference).

Ask "What would you like to do first?":

- **Start a project**: ask "What do you want to build?", then continue as `ooo interview`.
- **Try the tutorial**: continue as `ooo tutorial`.
- **Read the docs**: continue as `ooo help`.

Run Step 4 before handing off, so completion is recorded even when the next
command takes over the conversation.

### Step 4: GitHub Star and Completion

Run `gh auth status &>/dev/null && echo GH_OK || echo GH_MISSING`. If `GH_OK`
and `star_asked` is not true in `~/.ouroboros/prefs.json`, ask "If you're
enjoying Ouroboros, would you like to star it on GitHub?" with **Star on
GitHub** (run `gh api -X PUT /user/starred/Q00/ouroboros`) and **Maybe later**.

Then merge the completion fields into `~/.ouroboros/prefs.json`, keeping
existing keys. Prefix the command with `STAR_ASKED=true` when the star question
was asked, so it is never repeated:

```bash
ouroboros_python - < "$WELCOME_DIR/scripts/record_welcome_completed.py"
```

Before the handoff, mention that phrases like "interview me", "I'm stuck", or "evaluate this" also
route to the matching command; to watch `ooo run` or `ooo evolve` live, run
`uvx --python '>=3.12' --from 'ouroboros-ai[tui]' ouroboros tui monitor` in a
separate terminal.

## RFC #1392 State Breadcrumb Footer

End the final response with exactly one line `◆ <current state> → next: <recommended action>`. Derive the state from live session state via `ouroboros_session_status` when available, otherwise from this skill's actual outcome. Never use a linear `Step N of M` footer; when the next step is a real choice, list 2-3 options.
