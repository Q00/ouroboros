from ouroboros.observability.spend import (
    format_stage_breakdown,
    normalize_spend_stage,
    normalize_stage_breakdown,
    single_stage_breakdown,
)


def test_normalize_spend_stage_aliases() -> None:
    assert normalize_spend_stage("interview") == "interview"
    assert normalize_spend_stage("Deliver") == "execute"
    assert normalize_spend_stage("stage3") == "consensus"
    assert normalize_spend_stage("unknown") is None


def test_normalize_stage_breakdown_filters_unknown_and_empty_rows() -> None:
    assert normalize_stage_breakdown(
        {
            "Deliver": {"tokens": 1500, "cost_usd": 0.04},
            "stage3": {"tokens": 250, "cost": 0.02},
            "unknown": {"tokens": 999, "cost_usd": 9.99},
            "evaluate": {"tokens": 0, "cost_usd": 0.0},
        }
    ) == {
        "execute": {"tokens": 1500, "cost_usd": 0.04},
        "consensus": {"tokens": 250, "cost_usd": 0.02},
    }


def test_single_stage_breakdown_and_formatting() -> None:
    breakdown = single_stage_breakdown("Deliver", tokens=1200, cost_usd=0.045)

    assert breakdown == {"execute": {"tokens": 1200, "cost_usd": 0.045}}
    assert format_stage_breakdown(breakdown) == "execute $0.04/1.2K"
