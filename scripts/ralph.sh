#!/usr/bin/env bash
# ralph.sh — Loop evolve_step until convergence.
#
# Wraps scripts/ralph.py in a while-loop, piping JSON between cycles.
# Creates a git tag ooo/{lineage_id}/gen_{N} after each successful cycle.
#
# Exit codes:
#   0  — CONVERGED
#  10  — stagnation retry limit reached
#  11  — exhausted (max generations in evolve_step)
#  12  — failed (tool error)
#  14  — max cycles reached without convergence

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RALPH_PY="${SCRIPT_DIR}/ralph.py"

# ── Defaults ────────────────────────────────────────────────────────────────
LINEAGE_ID=""
SEED_FILE=""
MAX_CYCLES=30
MAX_RETRIES=2
NO_EXECUTE=false
NO_PARALLEL=false
NO_QA=false
SERVER_COMMAND=""
SERVER_ARGS=""
PROJECT_DIR=""

# ── Usage ───────────────────────────────────────────────────────────────────
usage() {
    cat <<'USAGE'
Usage: ralph.sh --lineage-id ID [OPTIONS]

Options:
  --lineage-id ID        Lineage identifier (required)
  --seed-file PATH       Seed YAML for Gen 1
  --max-cycles N         Max loop iterations (default: 30)
  --max-retries N        Lateral-think retries per stagnation (default: 2)
  --no-execute           Ontology-only evolution (skip execution)
  --no-parallel          Sequential AC execution (slower, more stable)
  --no-qa                Skip post-execution QA evaluation
  --server-command CMD   MCP server executable (default: ouroboros)
  --server-args ARGS     MCP server arguments (default: mcp)
  --project-dir PATH     Target repo/worktree for evolve_step and git snapshots
  -h, --help             Show this help

Exit codes:
   0  CONVERGED
  10  stagnation limit
  11  exhausted
  12  failed
  14  max cycles
USAGE
    exit 0
}

# ── Parse args ──────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --lineage-id)   LINEAGE_ID="$2"; shift 2 ;;
        --seed-file)    SEED_FILE="$2"; shift 2 ;;
        --max-cycles)   MAX_CYCLES="$2"; shift 2 ;;
        --max-retries)  MAX_RETRIES="$2"; shift 2 ;;
        --no-execute)   NO_EXECUTE=true; shift ;;
        --no-parallel)  NO_PARALLEL=true; shift ;;
        --no-qa)        NO_QA=true; shift ;;
        --server-command) SERVER_COMMAND="$2"; shift 2 ;;
        --project-dir)  PROJECT_DIR="$2"; shift 2 ;;
        --server-args)  shift; SERVER_ARGS="$*"; break ;;
        -h|--help)      usage ;;
        *)              echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

if [[ -z "$LINEAGE_ID" ]]; then
    echo "Error: --lineage-id is required" >&2
    exit 2
fi

if [[ -n "$PROJECT_DIR" ]]; then
    if [[ ! -d "$PROJECT_DIR" ]]; then
        echo "Error: --project-dir does not exist or is not a directory: $PROJECT_DIR" >&2
        exit 2
    fi
    PROJECT_DIR="$(cd "$PROJECT_DIR" && pwd -P)"
    if ! PROJECT_REPO_ROOT="$(git -C "$PROJECT_DIR" rev-parse --show-toplevel 2>/dev/null)"; then
        echo "Error: --project-dir must be inside a git worktree: $PROJECT_DIR" >&2
        exit 2
    fi
    PROJECT_DIR="$(cd "$PROJECT_REPO_ROOT" && pwd -P)"
fi

# ── Helpers ─────────────────────────────────────────────────────────────────
log() {
    echo "[ralph] $(date '+%H:%M:%S') $*" >&2
}

git_in_project() {
    if [[ -n "$PROJECT_DIR" ]]; then
        git -C "$PROJECT_DIR" "$@"
    else
        git "$@"
    fi
}

# Commit changes and create a git tag for the generation.
# Skipped when --no-execute (no code changes to snapshot).
tag_generation() {
    local gen="$1"
    local tag="ooo/${LINEAGE_ID}/gen_${gen}"

    if [[ "$NO_EXECUTE" == "true" ]]; then
        return 0
    fi

    if ! git_in_project rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        return 0
    fi

    # Auto-commit: commit any changes from this generation. Keep the dirty
    # predicate explicit so clean trees are not reported as committed and
    # staged-only changes still trigger a snapshot.
    if ! git_in_project diff --quiet HEAD 2>/dev/null || \
       ! git_in_project diff --cached --quiet 2>/dev/null || \
       [ -n "$(git_in_project ls-files --others --exclude-standard 2>/dev/null)" ]; then
        git_in_project add -A >/dev/null 2>&1 || true
        if git_in_project commit -m "ooo: gen ${gen} [${LINEAGE_ID}]" >/dev/null 2>&1; then
            log "Committed changes for gen ${gen}"
        fi
    fi

    # Overwrite tag if it already exists (re-run scenario)
    git_in_project tag -f "$tag" >/dev/null 2>&1 || true
    log "Tagged ${tag}"
}

# Rollback working tree to previous generation on failure.
rollback_to_previous() {
    local current_gen="$1"
    local prev_gen=$((current_gen - 1))

    if (( prev_gen < 1 )); then
        log "No previous generation to rollback to"
        return 0
    fi

    if [[ "$NO_EXECUTE" == "true" ]]; then
        return 0
    fi

    if ! git_in_project rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        return 0
    fi

    local prev_tag="ooo/${LINEAGE_ID}/gen_${prev_gen}"
    if git_in_project rev-parse "$prev_tag" >/dev/null 2>&1; then
        log "Rolling back to ${prev_tag} after failure"
        git_in_project checkout "$prev_tag" -- . >/dev/null 2>&1 || {
            log "WARNING: rollback to ${prev_tag} failed"
            return 0
        }
        git_in_project reset HEAD >/dev/null 2>&1 || true
        git_in_project clean -fd >/dev/null 2>&1 || true
        log "Rollback complete"
    else
        log "No tag ${prev_tag} found, skipping rollback"
    fi
}

# ── Build common python args ────────────────────────────────────────────────
build_py_args() {
    py_args=("--lineage-id" "$LINEAGE_ID" "--max-retries" "$MAX_RETRIES")

    if [[ "$NO_EXECUTE" == "true" ]]; then
        py_args+=("--no-execute")
    fi
    if [[ "$NO_PARALLEL" == "true" ]]; then
        py_args+=("--no-parallel")
    fi
    if [[ "$NO_QA" == "true" ]]; then
        py_args+=("--no-qa")
    fi
    if [[ -n "$SERVER_COMMAND" ]]; then
        py_args+=("--server-command" "$SERVER_COMMAND")
    fi
    if [[ -n "$PROJECT_DIR" ]]; then
        py_args+=("--project-dir" "$PROJECT_DIR")
    fi
    # NOTE: --server-args is NOT included here.
    # It uses REMAINDER and must be appended LAST in the main loop.

}

# ── Main loop ───────────────────────────────────────────────────────────────
cycle=0
stagnation_count=0

log "Starting Ralph loop for lineage=${LINEAGE_ID} max_cycles=${MAX_CYCLES} project_dir=${PROJECT_DIR:-$PWD}"

while (( cycle < MAX_CYCLES )); do
    cycle=$((cycle + 1))

    # Build per-cycle args
    build_py_args

    # Cycle 1: include seed file; Cycle 2+: omit it
    if (( cycle == 1 )) && [[ -n "$SEED_FILE" ]]; then
        py_args+=("--seed-file" "$SEED_FILE")
    fi

    # --server-args MUST be last (REMAINDER captures everything after it)
    if [[ -n "$SERVER_ARGS" ]]; then
        py_args+=("--server-args" $SERVER_ARGS)
    fi

    log "Cycle ${cycle}/${MAX_CYCLES} ..."

    # Run ralph.py — capture stdout (JSON) and exit code
    set +e
    output=$(python3 "$RALPH_PY" "${py_args[@]}")
    py_exit=$?
    set -e

    # On connection failure, abort immediately
    if (( py_exit == 1 )); then
        log "MCP connection failed"
        echo "$output"
        exit 12
    fi

    # Parse JSON fields
    action=$(echo "$output" | python3 -c "import sys,json; print(json.load(sys.stdin).get('action',''))" 2>/dev/null || echo "")
    generation=$(echo "$output" | python3 -c "import sys,json; print(json.load(sys.stdin).get('generation',''))" 2>/dev/null || echo "")
    similarity=$(echo "$output" | python3 -c "import sys,json; print(json.load(sys.stdin).get('similarity',''))" 2>/dev/null || echo "")
    error_msg=$(echo "$output" | python3 -c "import sys,json; print(json.load(sys.stdin).get('error','') or '')" 2>/dev/null || echo "")
    qa_verdict=$(echo "$output" | python3 -c "import sys,json; q=json.load(sys.stdin).get('qa'); print(q.get('verdict','') if q else '')" 2>/dev/null || echo "")
    qa_score=$(echo "$output" | python3 -c "import sys,json; q=json.load(sys.stdin).get('qa'); print(q.get('score','') if q else '')" 2>/dev/null || echo "")

    log "  action=${action} gen=${generation} sim=${similarity}"
    if [[ -n "$qa_verdict" ]]; then
        log "  QA: verdict=${qa_verdict} score=${qa_score}"
    fi

    # Tag the generation (skip on failure — rollback handles that case)
    if [[ -n "$generation" ]] && [[ "$generation" != "None" ]] && [[ "$action" != "failed" ]]; then
        tag_generation "$generation"
    fi

    case "$action" in
        continue)
            stagnation_count=0
            ;;
        converged)
            log "CONVERGED at generation ${generation} (similarity=${similarity})"
            echo "$output"
            exit 0
            ;;
        stagnated)
            stagnation_count=$((stagnation_count + 1))
            log "  Stagnation #${stagnation_count} (lateral_think already applied by ralph.py)"
            # ralph.py already did max_retries lateral_think attempts.
            # If still stagnated after that, we count it here.
            if (( stagnation_count >= MAX_RETRIES )); then
                log "Stagnation limit reached (${stagnation_count}/${MAX_RETRIES})"
                echo "$output"
                exit 10
            fi
            ;;
        exhausted)
            log "EXHAUSTED — max generations reached in evolve_step"
            echo "$output"
            exit 11
            ;;
        failed)
            log "FAILED: ${error_msg}"
            if [[ -n "$generation" ]] && [[ "$generation" != "None" ]]; then
                rollback_to_previous "$generation"
            fi
            echo "$output"
            exit 12
            ;;
        *)
            log "Unknown action '${action}', treating as failure"
            echo "$output"
            exit 12
            ;;
    esac
done

log "Max cycles (${MAX_CYCLES}) reached without convergence"
echo "$output"
exit 14
