""" "Suggest an answer" drafts in the goal's language and says so when it could not (#3050).

No agent CLI runs: the adapter is a fake that replies with scripted text and
records each prompt it was sent.
"""

from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any

import pytest

from ouroboros.core.types import Result
from ouroboros.gui import harness
from ouroboros.gui.answer_language import goal_language, in_language, plain_answer
from ouroboros.providers.base import CompletionResponse, UsageInfo

_RUSSIAN = "Используем SQLite и храним данные локально."


class FakeAgent:
    """Replies with ``replies`` in order (the last one repeats) and records each prompt."""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def __call__(self, **_kwargs: Any) -> FakeAgent:
        return self

    async def complete(self, messages: Any, config: Any) -> Any:
        self.prompts.append(messages[-1].content)
        text = self.replies[min(len(self.prompts), len(self.replies)) - 1]
        return Result.ok(CompletionResponse(content=text, model="m", usage=UsageInfo(0, 0, 0)))


def _suggest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, goal: str, agent: FakeAgent) -> dict:
    from ouroboros.config import loader
    from ouroboros.providers import factory

    monkeypatch.setattr(loader, "get_llm_model_for_role", lambda *_a, **_k: "m")
    monkeypatch.setattr(factory, "create_llm_adapter", agent)
    h = harness.Harness()
    h._sessions["c1"] = {
        "id": "c1",
        "title": goal,
        "goal": goal,
        "cwd": str(tmp_path),
        "mode": "interview",
        "project": "new",  # no repository to read: the model drafts the answer
        "backend": None,
        "status": "waiting_answer",
        "last_question": "Where is the data kept?",
        "version": 0,
        "messages": [{"id": "q1", "role": "assistant", "kind": "question", "text": "Where?"}],
    }
    h.action("c1", "suggest", {})
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        suggestion = (h.get("c1") or {}).get("suggestion") or {}
        if not suggestion.get("pending"):
            return suggestion
        time.sleep(0.02)
    raise AssertionError(json.dumps(h.get("c1"), default=str)[:2000])


# ── the language check itself ────────────────────────────────────────


@pytest.mark.parametrize(
    ("goal", "language"),
    [
        ("Build a CSV to Markdown converter", "en"),
        ("Construir un conversor de CSV", "en"),  # Latin script reads as English
        ("CSV를 마크다운으로 바꾸는 CLI", "ko"),
        ("CSVをMarkdownに変換するツール", "ja"),
        ("把 CSV 转成 Markdown 的工具", "zh"),
        ("Конвертер CSV в Markdown", "ru"),
        ("", "en"),
    ],
)
def test_the_goal_language_comes_from_its_script(goal: str, language: str) -> None:
    assert goal_language(goal) == language


def test_a_reply_must_be_in_the_language_and_no_other() -> None:
    assert in_language("Keep the data in SQLite.", "en")
    assert not in_language(_RUSSIAN, "en")
    assert not in_language(
        f"Keep the data in SQLite. {_RUSSIAN}", "en"
    )  # an English answer and a Russian copy
    assert in_language("데이터는 SQLite에 저장해요.", "ko")
    assert not in_language("Keep the data in SQLite.", "ko")
    assert in_language(
        "Run `pnpm build`, named `빌드` in the docs.", "en"
    )  # code may use any script
    assert not in_language("", "en")


def test_escaped_and_real_line_breaks_become_spaces() -> None:
    assert (
        plain_answer("Use SQLite.\\nKeep it local.\n\nNo server.")
        == "Use SQLite. Keep it local. No server."
    )


# ── through the console's suggest action ─────────────────────────────


def test_an_english_goal_gets_an_english_suggestion_and_the_prompt_names_english(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent = FakeAgent("Keep the data in a local SQLite file.")
    suggestion = _suggest(monkeypatch, tmp_path, "Build a notes app", agent)

    assert suggestion["text"] == "Keep the data in a local SQLite file."
    assert suggestion["source"] == "model"
    assert "language_mismatch" not in suggestion
    assert len(agent.prompts) == 1
    assert "Write the answer in English only." in agent.prompts[0]
    assert "one to three short sentences" in agent.prompts[0]


def test_a_russian_reply_to_an_english_goal_is_asked_for_again_in_english(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent = FakeAgent(_RUSSIAN, "Keep the data in a local SQLite file.")
    suggestion = _suggest(monkeypatch, tmp_path, "Build a notes app", agent)

    assert suggestion["text"] == "Keep the data in a local SQLite file."
    assert "language_mismatch" not in suggestion
    assert len(agent.prompts) == 2
    assert (
        "The previous draft was not in English. Write it again in English only." in agent.prompts[1]
    )


def test_a_second_miss_is_shown_with_a_note_not_hidden(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent = FakeAgent(_RUSSIAN)
    suggestion = _suggest(monkeypatch, tmp_path, "Build a notes app", agent)

    assert suggestion["text"] == _RUSSIAN
    assert suggestion["language_mismatch"] is True
    assert len(agent.prompts) == 2  # one retry, no more


def test_a_korean_goal_stays_korean(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    agent = FakeAgent("Keep the data in SQLite.", "데이터는 로컬 SQLite 파일에 저장해요.")
    suggestion = _suggest(monkeypatch, tmp_path, "메모 앱을 만들어 줘", agent)

    assert suggestion["text"] == "데이터는 로컬 SQLite 파일에 저장해요."
    assert "language_mismatch" not in suggestion
    assert "Write the answer in Korean only." in agent.prompts[0]
    assert len(agent.prompts) == 2


def test_escaped_line_breaks_in_a_reply_become_spaces(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent = FakeAgent("Use SQLite.\\nKeep it local.")
    suggestion = _suggest(monkeypatch, tmp_path, "Build a notes app", agent)

    assert suggestion["text"] == "Use SQLite. Keep it local."


def test_an_empty_reply_is_an_error_not_a_blank_suggestion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent = FakeAgent("  \\n ")
    suggestion = _suggest(monkeypatch, tmp_path, "Build a notes app", agent)

    assert suggestion.get("text") is None
    assert suggestion["error"] == "The agent returned an empty answer."
    assert len(agent.prompts) == 2


def test_every_catalog_has_the_different_language_note() -> None:
    import re

    repo = Path(__file__).resolve().parents[3]
    for lang in ("en", "ko", "zh", "ja", "es"):
        text = (repo / "gui" / "src" / "i18n" / f"{lang}.ts").read_text(encoding="utf-8")
        assert re.search(r'^\s*"chat\.suggest\.otherLanguage": "[^"]+",$', text, flags=re.M), lang
