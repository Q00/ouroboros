"""A model the agent refuses becomes a structured ``model_rejected`` chat message.

No agent CLI runs: adapters are fakes that return the exact error Codex sends
for a model a ChatGPT account cannot use.
"""

from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any

import pytest

from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result
from ouroboros.gui import api, harness, model_refusal
from ouroboros.providers.base import CompletionResponse, UsageInfo

_REFUSED = "gpt-6.2-astra"
_REASON = f"The '{_REFUSED}' model is not supported when using Codex with a ChatGPT account."


def _codex_refusal(model: str = _REFUSED) -> ProviderError:
    body = json.dumps(
        {
            "error": {
                "type": "invalid_request_error",
                "message": f"The '{model}' model is not supported when using Codex with a "
                "ChatGPT account.",
                "code": "model_not_found",
            }
        }
    )
    return ProviderError(
        body,
        provider="codex_cli",
        details={
            "returncode": 1,
            "session_id": "019a8c2e",
            "stderr": "Reading prompt from stdin...\nERROR codex_api::endpoint::responses_websocket",
            "stdout_errors": [body],
        },
    )


def _ok(text: str = "hi", model: str = "default") -> Any:
    return Result.ok(CompletionResponse(content=text, model=model, usage=UsageInfo(0, 0, 0)))


class FakeCodex:
    """Refuses ``refused`` models with Codex's error; answers any other."""

    def __init__(self, refused: set[str] | None = None, error: ProviderError | None = None) -> None:
        self.refused = {_REFUSED} if refused is None else refused
        self.error = error
        self.calls: list[tuple[str, list[str]]] = []

    def __call__(self, **_kwargs: Any) -> FakeCodex:
        return self

    async def complete(self, messages: Any, config: Any) -> Any:
        self.calls.append((config.model, [message.content for message in messages]))
        if self.error is not None:
            return Result.err(self.error)
        if config.model in self.refused:
            return Result.err(_codex_refusal(config.model))
        return _ok()


@pytest.fixture
def codex(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeCodex:
    from ouroboros.backends import model_catalog
    from ouroboros.providers import factory

    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(model_catalog, "detect_backend_cli", lambda _name: "/bin/codex")
    fake = FakeCodex()
    monkeypatch.setattr(factory, "create_llm_adapter", fake)
    return fake


def _wait(h: harness.Harness, chat_id: str, check: Any, timeout: float = 20.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        session = h.get(chat_id) or {}
        if check(session):
            return session
        time.sleep(0.02)
    raise AssertionError(f"timed out; last state: {json.dumps(h.get(chat_id), default=str)[:3000]}")


def _ask(h: harness.Harness, tmp_path: Path, model: str = _REFUSED) -> dict[str, Any]:
    chat = h.create(
        {"goal": "hello?", "mode": "ask", "backend": "codex", "model": model, "cwd": str(tmp_path)}
    )
    return _wait(h, chat["id"], lambda s: s.get("status") == "failed")


# ── Ask ──────────────────────────────────────────────────────────────


def test_a_refused_ask_model_is_a_structured_message_not_a_raw_blob(
    codex: FakeCodex, tmp_path: Path
) -> None:
    session = _ask(harness.Harness(), tmp_path)

    error = session["messages"][-1]
    assert (error["kind"], error["code"]) == ("error", "model_rejected")
    assert error["rejection"] == {
        "backend": "codex",
        "model": _REFUSED,
        "reason": _REASON,
        "reason_code": "chatgpt_account",
        "source": "chat",
    }
    assert error["text"] == f"codex didn't accept the model {_REFUSED}: {_REASON}"
    for raw in ("stderr", "details", "returncode", "{"):
        assert raw not in error["text"]
    # The raw error is kept for the details disclosure, bounded.
    assert "Reading prompt from stdin" in error["detail"]
    assert len(error["detail"]) <= model_refusal.MAX_DETAIL_CHARS
    assert session["model"] == _REFUSED  # never changed without a click


def test_retry_resends_the_same_question_on_the_agent_default(
    codex: FakeCodex, tmp_path: Path
) -> None:
    h = harness.Harness()
    chat_id = _ask(h, tmp_path)["id"]

    after_click = h.action(chat_id, "retry_default", {})

    assert after_click["model"] is None
    note = after_click["messages"][-1]
    assert (note["kind"], note["key"], note["vars"]) == (
        "note",
        "retried_default_model",
        {"model": _REFUSED},
    )
    session = _wait(h, chat_id, lambda s: s.get("status") == "waiting_answer")
    assert session["messages"][-1]["text"] == "hi"
    # Same question, first on the chosen model, then with the model unset (Codex: "default").
    assert [model for model, _ in codex.calls] == [_REFUSED, "default"]
    assert codex.calls[0][1] == codex.calls[1][1]
    assert codex.calls[1][1][-1] == "hello?"
    # The click is spent: there is nothing left to retry.
    with pytest.raises(harness.Refusal) as refused:
        h.action(chat_id, "retry_default", {})
    assert refused.value.code == "no_model_rejection"


def test_retry_that_is_refused_again_offers_nothing_more(
    codex: FakeCodex, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Agents & models pins the same model, so unsetting the chat's model resolves to it."""
    from ouroboros.config import loader

    monkeypatch.setattr(loader, "get_llm_model_for_role", lambda *_a, **_k: _REFUSED)
    h = harness.Harness()
    chat_id = _ask(h, tmp_path)["id"]

    h.action(chat_id, "retry_default", {})
    session = _wait(
        h, chat_id, lambda s: s.get("status") == "failed" and s["messages"][-1]["kind"] == "error"
    )

    assert session["messages"][-1]["rejection"]["source"] == "settings"
    assert model_refusal.retryable_model(session) is None
    with pytest.raises(harness.Refusal):
        h.action(chat_id, "retry_default", {})


def test_a_model_from_agents_and_models_is_marked_so_and_not_retried(
    codex: FakeCodex, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ouroboros.config import loader

    monkeypatch.setattr(loader, "get_llm_model_for_role", lambda *_a, **_k: "gpt-6.1-sol")
    codex.refused = {"gpt-6.1-sol"}
    h = harness.Harness()

    session = _ask(h, tmp_path, model="")

    rejection = session["messages"][-1]["rejection"]
    assert (rejection["model"], rejection["source"]) == ("gpt-6.1-sol", "settings")
    with pytest.raises(harness.Refusal) as refused:
        h.action(session["id"], "retry_default", {})
    assert refused.value.code == "no_model_rejection"


def test_another_failure_is_reported_as_before(codex: FakeCodex, tmp_path: Path) -> None:
    codex.error = ProviderError("Rate limit reached for gpt-6-sol", provider="codex_cli")
    h = harness.Harness()

    session = _ask(h, tmp_path, model="gpt-6-sol")

    error = session["messages"][-1]
    assert error["kind"] == "error" and "code" not in error and "rejection" not in error
    assert error["text"] == "Rate limit reached for gpt-6-sol"
    with pytest.raises(harness.Refusal):
        h.action(session["id"], "retry_default", {})


def test_typing_instead_of_clicking_keeps_the_chosen_model(
    codex: FakeCodex, tmp_path: Path
) -> None:
    h = harness.Harness()
    chat_id = _ask(h, tmp_path)["id"]

    h.send(chat_id, "and now?")
    session = _wait(h, chat_id, lambda s: s.get("status") == "failed" and len(s["messages"]) == 4)

    assert [model for model, _ in codex.calls] == [_REFUSED, _REFUSED]
    assert session["model"] == _REFUSED
    # The new refusal is the one a click would retry.
    assert session["messages"][-1]["code"] == "model_rejected"
    assert model_refusal.retryable_model(session) == _REFUSED


def test_the_retry_action_reaches_the_page_with_its_code(codex: FakeCodex, tmp_path: Path) -> None:
    state = api.ConsoleState(stop=lambda *_args, **_kwargs: None)
    chat = api.post_chat_session(
        state,
        query={},
        body={
            "goal": "hi",
            "mode": "ask",
            "backend": "codex",
            "model": "gpt-6-sol",
            "cwd": str(tmp_path),
        },
    )
    _wait(state.harness, chat["id"], lambda s: s.get("status") == "waiting_answer")

    with pytest.raises(api.ApiError) as refused:
        api.post_chat_action(state, chat["id"], query={}, body={"action": "retry_default"})

    assert (refused.value.status, refused.value.code) == (409, "no_model_rejection")


@pytest.mark.parametrize(
    ("mutate", "retryable"),
    [
        (lambda _s: None, True),
        (lambda s: s.update(mode="interview"), False),
        (lambda s: s.update(status="waiting_answer"), False),
        (lambda s: s.update(model=None), False),
        (lambda s: s["messages"].append({"kind": "text", "role": "user", "text": "x"}), False),
        (lambda s: s["messages"][-1]["rejection"].update(source="settings"), False),
        (lambda s: s["messages"][-1].pop("code"), False),
        (lambda s: s["messages"][-1].update(rejection="codex"), False),
        (lambda s: s.update(messages=[]), False),
    ],
)
def test_retryable_only_for_the_latest_refusal_of_the_chats_own_model(
    mutate: Any, retryable: bool
) -> None:
    session: dict[str, Any] = {
        "mode": "ask",
        "status": "failed",
        "model": _REFUSED,
        "messages": [
            {"kind": "text", "role": "user", "text": "hello?"},
            {"kind": "error", "code": "model_rejected", "rejection": {"source": "chat"}},
        ],
    }
    mutate(session)

    assert (model_refusal.retryable_model(session) == _REFUSED) is retryable


# ── Interview, Seed and Suggest an answer ─────────────────────────────


class RefusingHandlers:
    """Stands in for ``harness._Handlers``: each call asks the chat's agent through
    ``_FolderAdapter``, then fails the way the real handlers do."""

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def ensure(self) -> None:
        return None

    @property
    def interview(self) -> RefusingHandlers:
        return self

    async def _complete(self, *models: str) -> None:
        from ouroboros.providers.base import CompletionConfig, Message, MessageRole

        adapter = harness._FolderAdapter("interview", "codex")
        for model in models:
            await adapter.complete(
                [Message(role=MessageRole.USER, content="q")], CompletionConfig(model=model)
            )

    async def start(self, goal: str, *, cwd: str, interview_id: str | None = None) -> Any:
        from ouroboros.auto.adapters import HandlerError

        await self._complete(*self.calls)
        raise HandlerError("ouroboros_interview failed: Question generation failed: …")

    async def seed(self, interview_session_id: str, *, force: bool = False) -> Any:
        from ouroboros.auto.adapters import HandlerError

        await self._complete(*self.calls)
        raise HandlerError("ouroboros_generate_seed failed: Failed to generate seed: …")


@pytest.fixture
def folder_codex(monkeypatch: pytest.MonkeyPatch) -> FakeCodex:
    fake = FakeCodex(refused={"gpt-6.1-sol"})
    monkeypatch.setattr("ouroboros.providers.create_llm_adapter", fake)
    return fake


@pytest.mark.parametrize(
    ("calls", "rejected"),
    [
        (["gpt-6.1-sol"], True),
        (["gpt-6-sol", "gpt-6.1-sol"], True),
        # A later success clears the refusal: the failure is the handler's own.
        (["gpt-6.1-sol", "gpt-6-sol"], False),
        ([], False),
    ],
)
def test_an_interview_turn_reports_its_refused_model_only_when_that_was_the_last_call(
    folder_codex: FakeCodex,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    calls: list[str],
    rejected: bool,
) -> None:
    monkeypatch.setattr(harness.Harness, "_handlers_for", lambda *_a: RefusingHandlers(calls))
    h = harness.Harness()

    chat = h.create({"goal": "build it", "cwd": str(tmp_path)})
    session = _wait(h, chat["id"], lambda s: s.get("status") == "failed")

    error = session["messages"][-1]
    if rejected:
        assert error["code"] == "model_rejected"
        assert error["rejection"] == {
            "backend": "codex",
            "model": "gpt-6.1-sol",
            "reason": "The 'gpt-6.1-sol' model is not supported when using Codex with a "
            "ChatGPT account.",
            "reason_code": "chatgpt_account",
            "source": "settings",
        }
        assert "model_not_found" in error["detail"]
    else:
        assert "code" not in error
        assert error["text"].startswith("ouroboros_interview failed")


def test_a_seed_turn_reports_its_refused_model(
    folder_codex: FakeCodex, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        harness.Harness, "_handlers_for", lambda *_a: RefusingHandlers(["gpt-6.1-sol"])
    )
    h = harness.Harness()
    h._sessions["c1"] = {
        "id": "c1",
        "title": "t",
        "goal": "t",
        "cwd": str(tmp_path),
        "mode": "interview",
        "status": "waiting_answer",
        "interview_session_id": "interview_1",
        "version": 0,
        "messages": [],
    }

    h.action("c1", "seed", {"force": True})
    session = _wait(h, "c1", lambda s: s.get("status") == "failed")

    assert session["messages"][-1]["code"] == "model_rejected"
    assert session["messages"][-1]["rejection"]["source"] == "settings"


def test_a_refusal_outside_a_chat_task_records_nothing(folder_codex: FakeCodex) -> None:
    import asyncio
    import contextvars

    from ouroboros.providers.base import CompletionConfig, Message, MessageRole

    async def call() -> Any:
        return await harness._FolderAdapter("interview", "codex").complete(
            [Message(role=MessageRole.USER, content="q")], CompletionConfig(model="gpt-6.1-sol")
        )

    result = contextvars.Context().run(asyncio.run, call())

    assert result.is_err  # the handler still gets the provider's error unchanged


def test_a_refused_suggestion_says_so_and_points_at_agents_and_models(
    codex: FakeCodex, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ouroboros.config import loader

    monkeypatch.setattr(loader, "get_llm_model_for_role", lambda *_a, **_k: "gpt-6.1-sol")
    codex.refused = {"gpt-6.1-sol"}
    h = harness.Harness()
    h._sessions["c1"] = {
        "id": "c1",
        "title": "t",
        "goal": "t",
        "cwd": str(tmp_path),
        "mode": "interview",
        "project": "new",
        "backend": "codex",
        "status": "waiting_answer",
        "last_question": "Who uses it?",
        "version": 0,
        "messages": [{"id": "q1", "role": "assistant", "kind": "question", "text": "Who uses it?"}],
    }

    h.action("c1", "suggest", {})
    session = _wait(h, "c1", lambda s: not (s.get("suggestion") or {}).get("pending"))

    suggestion = session["suggestion"]
    assert suggestion["rejection"]["model"] == "gpt-6.1-sol"
    assert suggestion["rejection"]["source"] == "settings"
    assert suggestion["error"].startswith("codex didn't accept the model gpt-6.1-sol")
    assert session["status"] == "waiting_answer"  # the interview itself goes on


def test_every_page_string_the_refusal_needs_exists_in_every_catalog() -> None:
    import re

    repo = Path(__file__).resolve().parents[3]
    catalogs = {
        lang: set(
            re.findall(
                r'^\s*"([^"]+)":',
                (repo / "gui" / "src" / "i18n" / f"{lang}.ts").read_text(encoding="utf-8"),
                flags=re.MULTILINE,
            )
        )
        for lang in ("en", "ko", "zh", "ja", "es")
    }
    needed = {
        "chat.rejected",
        "chat.rejected.noModel",
        "chat.rejected.agent",
        "chat.rejected.reason.chatgpt_account",
        "chat.rejected.retry",
        "chat.rejected.models",
        "chat.rejected.details",
        "chat.note.retried_default_model",
        "error.no_model_rejection",
        "models.availability",
    }
    for lang, keys in catalogs.items():
        assert needed <= keys, (lang, sorted(needed - keys))


def test_the_real_interview_handler_turn_reports_its_refused_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through ``InterviewHandler``: it wraps the provider error in its own text."""
    from ouroboros.backends import model_catalog
    from ouroboros.mcp.tools import execution_handlers, run_successors

    # The run and evaluation handlers are out of scope (and would probe the agent CLI).
    for module, name in (
        (execution_handlers, "ExecuteSeedHandler"),
        (execution_handlers, "StartExecuteSeedHandler"),
        (run_successors, "build_run_successor_handler"),
    ):
        monkeypatch.setattr(module, name, lambda **_kwargs: object())
    monkeypatch.setattr(model_catalog, "detect_backend_cli", lambda _name: "/bin/codex")
    fake = FakeCodex(error=_codex_refusal("gpt-6.1-sol"))
    monkeypatch.setattr("ouroboros.providers.create_llm_adapter", fake)
    h = harness.Harness()

    chat = h.create({"goal": "build a todo app", "backend": "codex", "cwd": str(tmp_path)})
    session = _wait(h, chat["id"], lambda s: s.get("status") == "failed")

    assert fake.calls, "the interview asked its agent"
    error = session["messages"][-1]
    assert error["code"] == "model_rejected", error["text"]
    assert error["rejection"]["backend"] == "codex"
    assert error["rejection"]["model"] == "gpt-6.1-sol"
    assert error["rejection"]["source"] == "settings"
