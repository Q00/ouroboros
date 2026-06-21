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
