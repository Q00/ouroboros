"""``--model`` and ``--pin-models`` for commands that run models.

Both flags set the process-wide choice the model resolver already reads
(``OUROBOROS_MODEL`` and ``OUROBOROS_PIN_MODELS``, see
``ouroboros.config.model_selection``), so every role in this invocation, and
any child process it starts, resolves through the same rules as the saved
``models.default`` / ``models.pin`` settings. The previous values come back
when the scope exits, so an in-process invocation does not leak its choice
into the next one. A flag the user did not give changes nothing, so saved
per-role ids on explicit backends (``litellm``, ``copilot``) stay in effect.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import os
from typing import Annotated

import typer

ModelOption = Annotated[
    str | None,
    typer.Option(
        "--model",
        "-m",
        help=(
            "Model for every role in this command: auto (latest of each role's tier), "
            "frugal, standard, frontier, or a model id. Tier names work on every "
            "backend; Claude aliases such as opus only on Claude backends. "
            "Default: models.default in config."
        ),
    ),
]

PinModelsOption = Annotated[
    bool | None,
    typer.Option(
        "--pin-models/--no-pin-models",
        help=(
            "Run the per-role model ids saved in config (research and reproducibility). "
            "Default: models.pin in config (off)."
        ),
    ),
]


@contextmanager
def model_options(model: str | None, pin_models: bool | None) -> Iterator[None]:
    """Make this invocation's model choice visible to the model resolver."""
    updates: dict[str, str] = {}
    if model is not None and model.strip():
        updates["OUROBOROS_MODEL"] = model.strip()
    if pin_models is not None:
        updates["OUROBOROS_PIN_MODELS"] = "1" if pin_models else "0"
    previous = {name: os.environ.get(name) for name in updates}
    os.environ.update(updates)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


__all__ = ["ModelOption", "PinModelsOption", "model_options"]
