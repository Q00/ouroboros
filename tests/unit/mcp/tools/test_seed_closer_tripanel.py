"""K3 — Seed-closer tri-panel fan-out + deterministic synthesis.

Covers:
- ``build_seed_closer_tripanel_fanout`` builds three lanes (closer / contrarian /
  gap_hunter) keyed by ``context.lane_id``.
- ``synthesize_seed_closer_tripanel`` gates on the closer verdict and blocks on a
  HIGH-severity contrarian / gap-hunter finding.
"""

from __future__ import annotations

import pytest

from ouroboros.mcp.tools.subagent import (
    build_seed_closer_tripanel_fanout,
    synthesize_seed_closer_tripanel,
)


class TestTripanelBuilder:
    def test_builds_three_lanes(self) -> None:
        payloads, correlation_key = build_seed_closer_tripanel_fanout(
            session_id="s1",
            seed_context="goal: do the thing",
            ambiguity_score=0.18,
        )
        assert correlation_key == "context.lane_id"
        lanes = [p.context["lane_id"] for p in payloads]
        assert lanes == ["closer", "contrarian", "gap_hunter"]
        # contrarian lane uses the contrarian persona agent.
        assert payloads[1].agent == "contrarian"

    def test_rejects_empty_inputs(self) -> None:
        with pytest.raises(ValueError, match="session_id must not be empty"):
            build_seed_closer_tripanel_fanout(session_id="", seed_context="x")
        with pytest.raises(ValueError, match="seed_context must not be empty"):
            build_seed_closer_tripanel_fanout(session_id="s", seed_context="")


class TestTripanelSynthesis:
    def test_all_clear_is_seed_ready(self) -> None:
        outcome = synthesize_seed_closer_tripanel(
            {
                "closer": {"verdict": "seed_ready", "reason": "all decisions settled"},
                "contrarian": {"severity": "low", "finding": "minor wording"},
                "gap_hunter": {"severity": "low", "finding": "nothing material"},
            }
        )
        assert outcome["seed_ready"] is True
        assert outcome["blocking_questions"] == []
        assert outcome["high_severity_lanes"] == []

    def test_high_gap_hunter_finding_blocks(self) -> None:
        outcome = synthesize_seed_closer_tripanel(
            {
                "closer": {"verdict": "seed_ready", "reason": "looks done"},
                "contrarian": {"severity": "low", "finding": "fine"},
                "gap_hunter": {
                    "severity": "high",
                    "finding": "no error-handling requirement",
                    "question": "How should the tool handle malformed input?",
                },
            }
        )
        assert outcome["seed_ready"] is False
        assert outcome["high_severity_lanes"] == ["gap_hunter"]
        assert "How should the tool handle malformed input?" in outcome["blocking_questions"]

    def test_closer_not_ready_gates_even_without_high_findings(self) -> None:
        outcome = synthesize_seed_closer_tripanel(
            {
                "closer": {
                    "verdict": "not_ready",
                    "reason": "success criteria still vague",
                    "blocking_question": "What is the measurable done condition?",
                },
                "contrarian": {"severity": "low", "finding": "fine"},
                "gap_hunter": {"severity": "medium", "finding": "soft gap"},
            }
        )
        assert outcome["seed_ready"] is False
        assert "What is the measurable done condition?" in outcome["blocking_questions"]

    def test_high_contrarian_finding_blocks(self) -> None:
        outcome = synthesize_seed_closer_tripanel(
            {
                "closer": {"verdict": "seed_ready", "reason": "ok"},
                "contrarian": {
                    "severity": "high",
                    "finding": "goal conflates two products",
                    "question": "Are these one deliverable or two?",
                },
                "gap_hunter": {"severity": "low", "finding": "ok"},
            }
        )
        assert outcome["seed_ready"] is False
        assert outcome["high_severity_lanes"] == ["contrarian"]

    def test_missing_lane_is_not_seed_ready(self) -> None:
        outcome = synthesize_seed_closer_tripanel(
            {"closer": {"verdict": "seed_ready", "reason": "ok"}}
        )
        assert outcome["seed_ready"] is False
        assert set(outcome["missing_lanes"]) == {"contrarian", "gap_hunter"}
