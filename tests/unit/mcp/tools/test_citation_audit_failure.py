"""Citation-audit failures must not look like panels without citations."""

from pathlib import Path

from ouroboros.mcp.tools import citation_check
from ouroboros.mcp.tools.subagent import (
    FanoutRegistry,
    build_subagent_payload,
    register_lateral_persona_fanout,
    submit_fanout_results,
)


def test_lateral_audit_failure_is_explicit_and_nonfatal(tmp_path: Path, monkeypatch) -> None:
    registry = FanoutRegistry(tmp_path)
    payload = build_subagent_payload(
        tool_name="ouroboros_lateral_think",
        title="researcher",
        prompt="x",
        agent="researcher",
        context={"persona": "researcher"},
    )
    fanout_id = register_lateral_persona_fanout(registry, session_id="s", payloads=[payload])

    def broken(_texts):
        raise RuntimeError("private diagnostic should not be returned")

    monkeypatch.setattr(citation_check, "audit_citations", broken)
    out = submit_fanout_results(
        registry,
        session_id="s",
        correlation_key="context.persona",
        fanout_id=fanout_id,
        results=[
            {
                "key": "researcher",
                "content": '```json\n{"external_sources":["https://example.com"]}\n```',
            }
        ],
    )
    assert out["status"] == "complete"
    assert out["citation_audit"]["status"] == "unavailable"
    assert out["citation_audit"]["unverified_present"] is True
    assert "private diagnostic" not in str(out)
