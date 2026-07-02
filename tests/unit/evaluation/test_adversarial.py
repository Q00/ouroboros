"""Adversarial QA class registry — canonical, renderable, prompt-wired."""

from __future__ import annotations

from ouroboros.evaluation.adversarial import (
    ADVERSARIAL_CLASSES,
    AdversarialClass,
    get_class,
    render_checklist,
)


class TestRegistry:
    def test_nine_canonical_classes_present(self) -> None:
        ids = {c.id for c in ADVERSARIAL_CLASSES}
        assert ids == {
            "malformed_input",
            "prompt_injection",
            "cancel_resume",
            "stale_state",
            "dirty_worktree",
            "hung_command",
            "flaky_test",
            "misleading_output",
            "repeated_interrupt",
        }

    def test_every_class_has_trigger_and_probe(self) -> None:
        for c in ADVERSARIAL_CLASSES:
            assert isinstance(c, AdversarialClass)
            assert c.id and c.name and c.trigger and c.probe

    def test_ids_are_unique(self) -> None:
        ids = [c.id for c in ADVERSARIAL_CLASSES]
        assert len(ids) == len(set(ids))

    def test_get_class_roundtrip(self) -> None:
        assert get_class("prompt_injection").name == "Prompt / instruction injection"
        assert get_class("nonexistent") is None


class TestRender:
    def test_checklist_lists_every_class_id(self) -> None:
        text = render_checklist()
        for c in ADVERSARIAL_CLASSES:
            assert c.id in text
        # trigger-conditional phrasing so the judge skips non-applicable classes
        assert "if " in text

    def test_render_subset(self) -> None:
        subset = (ADVERSARIAL_CLASSES[0],)
        text = render_checklist(subset)
        assert ADVERSARIAL_CLASSES[0].id in text
        assert ADVERSARIAL_CLASSES[1].id not in text

    def test_section_stamps_schema_version(self) -> None:
        from ouroboros.evaluation.adversarial import (
            ADVERSARIAL_SCHEMA_VERSION,
            render_adversarial_section,
        )

        assert f"schema v{ADVERSARIAL_SCHEMA_VERSION}" in render_adversarial_section()

    def test_section_code_mode_keeps_evidence_gap_contract(self) -> None:
        from ouroboros.evaluation.adversarial import render_adversarial_section

        text = render_adversarial_section("code")
        assert "evidence gap" in text
        assert "instead of implying you ran it" in text

    def test_section_document_mode_never_penalizes_unrunnable(self) -> None:
        from ouroboros.evaluation.adversarial import render_adversarial_section

        text = render_adversarial_section("document")
        # A spec cannot be executed; that must never be scored as missing evidence
        # (the seed-QA gate feeds artifact_type="document" on every auto run).
        assert "Never report an unrunnable probe as an evidence gap" in text
        assert "never lower any dimension" in text
        assert "completeness lens" in text
        assert "unverified applicable probe lower" not in text
        assert "instead of implying you ran it" not in text


class TestPromptWiring:
    def test_qa_user_prompt_includes_adversarial_section(self) -> None:
        from ouroboros.mcp.tools.qa import _build_qa_user_prompt

        prompt = _build_qa_user_prompt(
            artifact="def f(): pass",
            artifact_type="code",
            quality_bar="must work",
        )
        assert "Adversarial Probes" in prompt
        assert "malformed_input" in prompt
        assert "prompt_injection" in prompt
        assert "evidence gap" in prompt
        assert "instead of implying you ran it" in prompt

    def test_qa_user_prompt_document_mode_uses_completeness_contract(self) -> None:
        from ouroboros.mcp.tools.qa import _build_qa_user_prompt

        prompt = _build_qa_user_prompt(
            artifact="goal: ship\nacceptance_criteria: [works]",
            artifact_type="document",
            quality_bar="clear spec",
        )
        assert "Adversarial Probes" in prompt
        assert "completeness lens" in prompt
        assert "unverified applicable probe lower" not in prompt
        assert "instead of implying you ran it" not in prompt
