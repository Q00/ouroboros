"""Carry an agent's "I can't use this model" answer to the chat that asked.

The console's chat paths call an agent with a model. In Ask the chat may name
one; otherwise, as in Suggest an answer, the Interview and the Seed, it is the
stage's model from Agents & models. When the agent says it cannot use that
model, the chat shows a structured ``model_rejected`` message the page
translates, instead of the provider's raw error. The raw error is kept on the
message for the page's details disclosure.

Which errors count is decided by :mod:`ouroboros.providers.model_rejection`
alone. This module carries its answer to the chat:

* :func:`refusal` turns a provider error into ``(rejection, detail)``;
* :func:`note` records a refused call made inside a chat task, and
  :func:`watch` reports that task's failure as :class:`ModelRejected` when its
  last call was refused (the interview and Seed handlers wrap the provider
  error in their own text);
* :func:`retryable_model` says when sending an Ask turn again on the agent's
  default model can change anything.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
import contextvars
from typing import Any

MAX_DETAIL_CHARS = 4000

# The refusal the last LLM call of this chat task reported, if any. Set fresh by
# ``watch`` for each task. Child tasks and threads copy the context, so they
# share the same dict.
_BOX: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "gui_model_refusal", default=None
)


class ModelRejected(RuntimeError):
    """A chat turn the agent refused because of the model it was given.

    ``rejection`` is what the page shows:

    * ``backend``, the canonical name;
    * ``model``, or ``None`` when unknown;
    * ``reason``, the provider's one-line reason, and its ``reason_code``;
    * ``source``: ``"chat"`` when the chat named the model, ``"settings"``
      when it came from Agents & models.

    ``detail`` is the provider's raw error.
    """

    def __init__(self, rejection: dict[str, Any], detail: str) -> None:
        model = rejection.get("model")
        what = f"the model {model}" if model else "its model"
        super().__init__(f"{rejection['backend']} didn't accept {what}: {rejection['reason']}")
        self.rejection = rejection
        self.detail = detail

    def fields(self) -> dict[str, Any]:
        """The chat message fields that carry this refusal."""
        return {"code": "model_rejected", "rejection": self.rejection, "detail": self.detail}


def refusal(
    backend: str | None, error: object, model: str | None, source: str = "settings"
) -> tuple[dict[str, Any], str] | None:
    """``(rejection, detail)`` when ``error`` says the agent can't use its model, else ``None``.

    ``backend`` is the LLM backend the call went to (``None``: the configured
    one) and ``model`` the model it was asked for.
    """
    from ouroboros.backends import get_backend_capability
    from ouroboros.providers import resolve_llm_backend
    from ouroboros.providers.model_rejection import classify_model_rejection

    found = classify_model_rejection(error, requested_model=model)
    if found is None:
        return None
    try:
        capability = get_backend_capability(resolve_llm_backend(backend))
    except ValueError:
        capability = None
    rejection = {
        "backend": capability.name if capability else (backend or ""),
        "model": found.model,
        "reason": found.reason,
        "reason_code": found.reason_code,
        "source": source,
    }
    return rejection, str(error)[:MAX_DETAIL_CHARS]


def note(result: Any, config: Any, backend: Callable[[], str | None]) -> None:
    """Record whether this chat task's latest LLM call was refused for its model.

    Only the latest call counts: a later success, or a failure of another
    kind, clears an earlier refusal. Outside a watched task this does nothing.
    """
    box = _BOX.get()
    if box is None:
        return
    box.pop("found", None)
    if getattr(result, "is_err", False):
        found = refusal(backend(), result.error, getattr(config, "model", None))
        if found is not None:
            box["found"] = found


async def watch(coro: Coroutine[Any, Any, Any]) -> Any:
    """Run a chat task, and report its failure as ``ModelRejected`` when its last call was refused.

    Any other failure, including one whose last LLM call was not refused, is
    raised unchanged.
    """
    box: dict[str, Any] = {}
    _BOX.set(box)
    try:
        return await coro
    except ModelRejected:
        raise
    except Exception as exc:
        found = box.get("found")
        if found is None:
            raise
        raise ModelRejected(*found) from exc


def retryable_model(session: dict[str, Any]) -> str | None:
    """The model an Ask chat's last turn was refused for, when the chat itself named it.

    Only then can sending the turn again without it change anything. A model
    from Agents & models is what an unset chat model resolves to.
    """
    messages = session.get("messages") or []
    last = messages[-1] if messages else None
    if not isinstance(last, dict):
        return None
    rejection = last.get("rejection")
    model = session.get("model")
    if (
        session.get("mode") != "ask"
        or session.get("status") != "failed"
        or last.get("code") != "model_rejected"
        or not isinstance(rejection, dict)
        or rejection.get("source") != "chat"
        or not isinstance(model, str)
        or not model
    ):
        return None
    return model


__all__ = ["MAX_DETAIL_CHARS", "ModelRejected", "note", "refusal", "retryable_model", "watch"]
