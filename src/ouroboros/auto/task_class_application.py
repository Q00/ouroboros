"""L1-d: apply L1-a task-class default AC templates to a Seed.

The Socratic interview standardizes the ledger; :mod:`domain_inference`
(L1-b) classifies the ledger into a single :class:`TaskClass`; this
module wires that class's default acceptance-criteria template into the
:class:`ouroboros.core.seed.Seed` so the auto pipeline never ships a
Seed that lacks the class-appropriate runtime AC.

The helper is intentionally split out from the inference module so:

- The pipeline's call site stays small (one import, one function call).
- Tests can exercise the *application* path without exercising the
  *inference* path (and vice versa).
- A future contributor adding a new ``TaskClass`` only updates one
  catalog (``task_classes.py``); they do not also need to touch the
  inference and application modules.
"""

from __future__ import annotations

from dataclasses import dataclass

from ouroboros.auto.task_classes import TASK_CLASS_CATALOG, TaskClass
from ouroboros.core.seed import Seed

__all__ = [
    "AppliedTaskClassDefaults",
    "apply_default_ac_template",
]


@dataclass(frozen=True, slots=True)
class AppliedTaskClassDefaults:
    """Outcome of :func:`apply_default_ac_template`.

    Attributes
    ----------
    seed:
        The Seed instance after application. When ``injected_ac`` is
        empty, this is the *same* object the caller passed in (no
        ``model_copy`` is performed) so callers can detect a no-op
        without an equality dance.
    injected_ac:
        The acceptance-criteria entries prepended in this call —
        possibly a subset of the catalog template if some entries were
        already present verbatim in the original seed.
    task_class:
        The class whose defaults were applied (mirrors the caller's
        argument). Pinned on the result so the envelope and audit
        events can record exactly what fired.
    """

    seed: Seed
    injected_ac: tuple[str, ...]
    task_class: TaskClass


def apply_default_ac_template(seed: Seed, task_class: TaskClass) -> AppliedTaskClassDefaults:
    """Prepend the L1-a default AC template for *task_class* to *seed*.

    Behaviour:

    - Look up :attr:`TaskClassProfile.default_ac_template` in
      :data:`TASK_CLASS_CATALOG`.
    - For each template entry NOT already present (case-sensitive
      exact match) in ``seed.acceptance_criteria``, **prepend** to
      the seed's AC tuple via :meth:`Seed.model_copy`. (Prepend, not
      append, so the domain-baseline criteria appear before the
      user's specifics — they read as preconditions.)
    - If every template entry is already present, return the seed
      object *unchanged* (no ``model_copy`` allocation).
    - Empty template → no-op.

    Conflict-with-user-AC policy: user-supplied AC always wins on
    duplicates. We never *replace* a user criterion that happens to
    match a template entry; we just skip adding the template's copy.
    """
    if task_class not in TASK_CLASS_CATALOG:  # pragma: no cover - enum guarded
        msg = f"unknown TaskClass: {task_class!r}"
        raise KeyError(msg)

    profile = TASK_CLASS_CATALOG[task_class]
    template = profile.default_ac_template
    if not template:
        return AppliedTaskClassDefaults(seed=seed, injected_ac=(), task_class=task_class)

    existing = set(seed.acceptance_criteria)
    new_entries = tuple(item for item in template if item not in existing)
    if not new_entries:
        return AppliedTaskClassDefaults(seed=seed, injected_ac=(), task_class=task_class)

    updated_ac = new_entries + tuple(seed.acceptance_criteria)
    new_seed = seed.model_copy(update={"acceptance_criteria": updated_ac})
    return AppliedTaskClassDefaults(seed=new_seed, injected_ac=new_entries, task_class=task_class)
