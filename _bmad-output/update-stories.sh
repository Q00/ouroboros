#!/bin/bash
# Update all 31 story files to GitHub issues

set -e

REPO="Q00/ouroboros"
STORY_DIR="_bmad-output/implementation-artifacts/stories"

# Get current time
NOW=$(date -u +"%Y-%m-%dT%H:%M:%S+00:00")

echo "Starting GitHub issue updates..."
echo "Repository: $REPO"
echo

SUCCESS=0
FAILED=0

# Function to update issue
update_issue() {
    local STORY_FILE=$1
    local ISSUE_NUM=$2

    if [ ! -f "$STORY_FILE" ]; then
        echo "❌ File not found: $STORY_FILE"
        ((FAILED++))
        return
    fi

    STORY_TITLE=$(grep "^# " "$STORY_FILE" | head -1 | sed 's/^# //')
    STORY_CONTENT=$(cat "$STORY_FILE")

    echo "⏳ $(basename $STORY_FILE) -> Issue #$ISSUE_NUM"

    # Update the GitHub issue
    if gh issue edit "$ISSUE_NUM" --repo "$REPO" --body "$STORY_CONTENT" 2>/dev/null; then
        echo "✅ Updated issue #$ISSUE_NUM"
        ((SUCCESS++))
    else
        echo "❌ Failed to update issue #$ISSUE_NUM"
        ((FAILED++))
    fi
    echo
}

# Update all issues
update_issue "$STORY_DIR/0-1-project-initialization-with-uv.md" 22
update_issue "$STORY_DIR/0-2-core-types-and-error-handling.md" 25
update_issue "$STORY_DIR/0-3-event-store-with-sqlalchemy-core.md" 27
update_issue "$STORY_DIR/0-4-configuration-and-credentials-management.md" 31
update_issue "$STORY_DIR/0-5-llm-provider-adapter-with-litellm.md" 33
update_issue "$STORY_DIR/0-6-cli-skeleton-with-typer-and-rich.md" 35
update_issue "$STORY_DIR/0-7-structured-logging-with-structlog.md" 37
update_issue "$STORY_DIR/0-8-checkpoint-and-recovery-system.md" 38
update_issue "$STORY_DIR/0-9-context-compression-engine.md" 39
update_issue "$STORY_DIR/1-1-interview-protocol-engine.md" 12
update_issue "$STORY_DIR/1-2-ambiguity-score-calculation.md" 14
update_issue "$STORY_DIR/1-3-immutable-seed-generation.md" 16
update_issue "$STORY_DIR/2-1-three-tier-model-configuration.md" 18
update_issue "$STORY_DIR/2-2-complexity-based-routing.md" 20
update_issue "$STORY_DIR/2-3-escalation-on-failure.md" 23
update_issue "$STORY_DIR/2-4-downgrade-on-success.md" 26
update_issue "$STORY_DIR/3-1-double-diamond-cycle-implementation.md" 29
update_issue "$STORY_DIR/3-2-hierarchical-ac-decomposition.md" 32
update_issue "$STORY_DIR/3-3-atomicity-detection.md" 34
update_issue "$STORY_DIR/3-4-subagent-isolation.md" 36
update_issue "$STORY_DIR/4-1-stagnation-detection-4-patterns.md" 9
update_issue "$STORY_DIR/4-2-lateral-thinking-personas.md" 10
update_issue "$STORY_DIR/4-3-persona-rotation-strategy.md" 11
update_issue "$STORY_DIR/5-1-stage-1-mechanical-verification.md" 13
update_issue "$STORY_DIR/5-2-stage-2-semantic-evaluation.md" 15
update_issue "$STORY_DIR/5-3-stage-3-multi-model-consensus.md" 17
update_issue "$STORY_DIR/5-4-consensus-trigger-matrix.md" 19
update_issue "$STORY_DIR/6-1-drift-measurement-engine.md" 21
update_issue "$STORY_DIR/6-2-automatic-retrospective.md" 24
update_issue "$STORY_DIR/7-1-todo-registry.md" 28
update_issue "$STORY_DIR/7-2-secondary-loop-batch-processing.md" 30

echo "================================"
echo "Update Summary:"
echo "  Success: $SUCCESS"
echo "  Failed:  $FAILED"
echo "  Total:   31"
echo "================================"
