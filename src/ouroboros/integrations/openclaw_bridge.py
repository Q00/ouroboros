#!/usr/bin/env python3
"""
OpenClaw Bridge for Ouroboros

Thin CLI wrapper around Ouroboros core classes (InterviewEngine, AmbiguityScorer,
SeedGenerator) for use via OpenClaw agents over async message-based channels
(Telegram, WhatsApp, etc).

Each command does ONE step and exits — the agent calls them sequentially
between user messages.

Usage:
    # Start a new interview
    python -m ouroboros.integrations.openclaw_bridge start "I want to build a task management CLI"

    # Ask next question (reads state from session file)
    python -m ouroboros.integrations.openclaw_bridge ask <session-id>

    # Record user response
    python -m ouroboros.integrations.openclaw_bridge respond <session-id> "The tool should work offline"

    # Score ambiguity
    python -m ouroboros.integrations.openclaw_bridge score <session-id>

    # Generate seed spec
    python -m ouroboros.integrations.openclaw_bridge seed <session-id>

    # Show current state
    python -m ouroboros.integrations.openclaw_bridge status <session-id>

Environment variables:
    SOCRATIC_SESSIONS_DIR   Directory to store session files (default: ~/socratic-sessions)
    SOCRATIC_MODEL          LiteLLM model string to use (default: claude-sonnet-4-5)
    ANTHROPIC_API_KEY       Required for Anthropic/Claude models
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

import litellm

# litellm.modify_params allows LiteLLM to silently drop unsupported parameters
# (e.g. 'top_p' on providers that don't accept it) instead of raising an error.
# Required for cross-provider compatibility when routing between Anthropic, OpenAI, etc.
litellm.modify_params = True

from ouroboros.bigbang.ambiguity import (
    AMBIGUITY_THRESHOLD,
    AmbiguityScorer,
)
from ouroboros.bigbang.interview import (
    InterviewEngine,
    InterviewRound,
    InterviewState,
    InterviewStatus,
)
from ouroboros.bigbang.seed_generator import SeedGenerator
from ouroboros.providers.litellm_adapter import LiteLLMAdapter

# --- Config ---
SESSIONS_DIR = Path(
    os.environ.get(
        "SOCRATIC_SESSIONS_DIR", Path.home() / "socratic-sessions"
    )
)
DEFAULT_MODEL = os.environ.get("SOCRATIC_MODEL", "claude-sonnet-4-5")


def get_adapter() -> LiteLLMAdapter:
    """Create LiteLLM adapter (picks up ANTHROPIC_API_KEY from env)."""
    return LiteLLMAdapter(timeout=120.0)


def load_state(session_id: str) -> InterviewState:
    """Load interview state from session file."""
    state_file = SESSIONS_DIR / f"{session_id}.json"
    if not state_file.exists():
        print(f"ERROR: Session '{session_id}' not found at {state_file}", file=sys.stderr)
        sys.exit(1)
    return InterviewState.model_validate_json(state_file.read_text())


def save_state(state: InterviewState) -> None:
    """Save interview state to session file."""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    state_file = SESSIONS_DIR / f"{state.interview_id}.json"
    state_file.write_text(state.model_dump_json(indent=2))


# --- Commands ---


async def cmd_start(initial_context: str) -> None:
    """Start a new interview session."""
    adapter = get_adapter()
    engine = InterviewEngine(
        llm_adapter=adapter,
        state_dir=SESSIONS_DIR,
        model=DEFAULT_MODEL,
    )

    result = await engine.start_interview(initial_context)
    if result.is_err:
        print(f"ERROR: {result.error}", file=sys.stderr)
        sys.exit(1)

    state = result.value
    save_state(state)

    # Ask first question
    q_result = await engine.ask_next_question(state)
    if q_result.is_err:
        print(f"ERROR generating question: {q_result.error}", file=sys.stderr)
        sys.exit(1)

    question = q_result.value
    round_data = InterviewRound(
        round_number=state.current_round_number,
        question=question,
    )
    state.rounds.append(round_data)
    state.mark_updated()
    save_state(state)

    print(
        json.dumps(
            {
                "session_id": state.interview_id,
                "round": 1,
                "question": question,
                "status": "in_progress",
            },
            ensure_ascii=False,
            indent=2,
        )
    )


async def cmd_respond(session_id: str, response: str) -> None:
    """Record user response and ask next question."""
    adapter = get_adapter()
    engine = InterviewEngine(
        llm_adapter=adapter,
        state_dir=SESSIONS_DIR,
        model=DEFAULT_MODEL,
    )

    state = load_state(session_id)

    if state.status != InterviewStatus.IN_PROGRESS:
        print(f"ERROR: Interview is {state.status}", file=sys.stderr)
        sys.exit(1)

    # Record response on the last round
    if state.rounds and state.rounds[-1].user_response is None:
        state.rounds[-1].user_response = response
    else:
        print("ERROR: No pending question to respond to", file=sys.stderr)
        sys.exit(1)

    state.mark_updated()
    save_state(state)

    # Ask next question
    q_result = await engine.ask_next_question(state)
    if q_result.is_err:
        print(f"ERROR generating question: {q_result.error}", file=sys.stderr)
        sys.exit(1)

    question = q_result.value
    round_data = InterviewRound(
        round_number=state.current_round_number,
        question=question,
    )
    state.rounds.append(round_data)
    state.mark_updated()
    save_state(state)

    print(
        json.dumps(
            {
                "session_id": state.interview_id,
                "round": len(state.rounds),
                "question": question,
                "status": "in_progress",
            },
            ensure_ascii=False,
            indent=2,
        )
    )


async def cmd_score(session_id: str) -> None:
    """Score ambiguity of current interview state."""
    adapter = get_adapter()
    scorer = AmbiguityScorer(
        llm_adapter=adapter,
        model=DEFAULT_MODEL,
        temperature=0.1,
    )

    state = load_state(session_id)

    result = await scorer.score(state)
    if result.is_err:
        print(f"ERROR scoring: {result.error}", file=sys.stderr)
        sys.exit(1)

    score = result.value

    output = {
        "session_id": session_id,
        "ambiguity_score": round(score.overall_score, 3),
        "is_ready": score.is_ready_for_seed,
        "threshold": AMBIGUITY_THRESHOLD,
        "breakdown": {
            comp.name: {
                "clarity": round(comp.clarity_score, 3),
                "weight": comp.weight,
                "justification": comp.justification,
            }
            for comp in score.breakdown.components
        },
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


async def cmd_seed(session_id: str) -> None:
    """Generate seed specification from completed interview."""
    adapter = get_adapter()

    # First, score to check readiness
    scorer = AmbiguityScorer(
        llm_adapter=adapter,
        model=DEFAULT_MODEL,
        temperature=0.1,
    )
    state = load_state(session_id)

    score_result = await scorer.score(state)
    if score_result.is_err:
        print(f"ERROR scoring: {score_result.error}", file=sys.stderr)
        sys.exit(1)

    ambiguity = score_result.value
    if not ambiguity.is_ready_for_seed:
        print(
            json.dumps(
                {
                    "error": "Ambiguity too high",
                    "score": round(ambiguity.overall_score, 3),
                    "threshold": AMBIGUITY_THRESHOLD,
                    "message": f"Score {ambiguity.overall_score:.2f} > {AMBIGUITY_THRESHOLD}. Need more clarification.",
                },
                indent=2,
            )
        )
        sys.exit(1)

    # Mark interview complete
    state.status = InterviewStatus.COMPLETED
    state.mark_updated()
    save_state(state)

    # Generate seed
    generator = SeedGenerator(
        llm_adapter=adapter,
        model=DEFAULT_MODEL,
        output_dir=SESSIONS_DIR,
    )

    seed_result = await generator.generate(state=state, ambiguity_score=ambiguity)
    if seed_result.is_err:
        print(f"ERROR generating seed: {seed_result.error}", file=sys.stderr)
        sys.exit(1)

    seed = seed_result.value

    # Save seed YAML
    yaml_path = SESSIONS_DIR / f"{session_id}-seed.yaml"
    await generator.save_seed(seed, yaml_path)

    # Also output as JSON for the agent
    print(
        json.dumps(
            {
                "session_id": session_id,
                "seed_file": str(yaml_path),
                "goal": seed.goal,
                "constraints": list(seed.constraints),
                "acceptance_criteria": list(seed.acceptance_criteria),
                "ambiguity_score": round(ambiguity.overall_score, 3),
                "status": "seed_generated",
            },
            ensure_ascii=False,
            indent=2,
        )
    )


async def cmd_status(session_id: str) -> None:
    """Show current interview state."""
    state = load_state(session_id)

    rounds_summary = []
    for r in state.rounds:
        rounds_summary.append(
            {
                "round": r.round_number,
                "question": r.question[:100] + "..." if len(r.question) > 100 else r.question,
                "answered": r.user_response is not None,
            }
        )

    print(
        json.dumps(
            {
                "session_id": state.interview_id,
                "status": state.status,
                "rounds": len(state.rounds),
                "rounds_detail": rounds_summary,
                "initial_context": state.initial_context,
                "is_brownfield": state.is_brownfield,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


async def cmd_complete(session_id: str) -> None:
    """Mark interview as complete (user decided they're done)."""
    state = load_state(session_id)
    state.status = InterviewStatus.COMPLETED
    state.mark_updated()
    save_state(state)
    print(
        json.dumps(
            {
                "session_id": session_id,
                "status": "completed",
                "total_rounds": len(state.rounds),
            },
            indent=2,
        )
    )


# --- Main ---


def main():
    parser = argparse.ArgumentParser(description="OpenClaw Bridge for Ouroboros")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # start
    subparsers.add_parser("start", help="Start new interview").add_argument(
        "context", help="Initial project context/idea"
    )

    # respond
    p_respond = subparsers.add_parser("respond", help="Record response and get next question")
    p_respond.add_argument("session_id", help="Session ID")
    p_respond.add_argument("response", help="User's response")

    # score
    subparsers.add_parser("score", help="Score ambiguity").add_argument(
        "session_id", help="Session ID"
    )

    # seed
    subparsers.add_parser("seed", help="Generate seed spec").add_argument(
        "session_id", help="Session ID"
    )

    # status
    subparsers.add_parser("status", help="Show interview state").add_argument(
        "session_id", help="Session ID"
    )

    # complete
    subparsers.add_parser("complete", help="Mark interview as done").add_argument(
        "session_id", help="Session ID"
    )

    args = parser.parse_args()

    if args.command == "start":
        asyncio.run(cmd_start(args.context))
    elif args.command == "respond":
        asyncio.run(cmd_respond(args.session_id, args.response))
    elif args.command == "score":
        asyncio.run(cmd_score(args.session_id))
    elif args.command == "seed":
        asyncio.run(cmd_seed(args.session_id))
    elif args.command == "status":
        asyncio.run(cmd_status(args.session_id))
    elif args.command == "complete":
        asyncio.run(cmd_complete(args.session_id))


if __name__ == "__main__":
    main()
