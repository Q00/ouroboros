"""PM Seed — immutable specification for product requirements.

A PMSeed captures PM-level product requirements: goals, user stories,
constraints, success criteria, and decide-later items. It is produced by the
PM interview flow and can be serialized to YAML for handoff to a
development interview via initial_context.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class UserStory:
    """A single user story captured during PM interview.

    Attributes:
        persona: Who benefits (e.g., "PM", "Developer").
        action: What the user wants to do.
        benefit: Why they want to do it.
    """

    persona: str
    action: str
    benefit: str

    def __str__(self) -> str:
        return f"As a {self.persona}, I want to {self.action}, so that {self.benefit}."


@dataclass(frozen=True, slots=True)
class PMSeed:
    """Immutable product requirements seed produced by PM interview.

    This is the PM-facing counterpart of Seed. It captures product-level
    requirements before they are translated into development specifications.

    Attributes:
        pm_id: Unique identifier for this PM seed.
        product_name: Name of the product or feature.
        goal: High-level product goal statement.
        user_stories: Captured user stories.
        constraints: Product constraints (budget, timeline, compliance, etc.).
        success_criteria: Measurable success criteria.
        assumptions: Assumptions made during the interview.
        decide_later_items: Questions deferred or identified as premature.
        interview_id: Reference to the source PM interview.
        codebase_context: Shared codebase exploration context (brownfield).
        brownfield_repos: Registered brownfield repositories.
        created_at: When this seed was generated.

    Deprecated fields (kept for backward compatibility, merged on init):
        deferred_items: Merged into decide_later_items.
        deferred_decisions: Merged into decide_later_items.
        seed: Preserved for legacy round-trip; not used by new code.
        referenced_repos: Merged into brownfield_repos.
    """

    pm_id: str = field(default_factory=lambda: f"pm_seed_{uuid4().hex[:12]}")
    product_name: str = ""
    goal: str = ""
    user_stories: tuple[UserStory, ...] = ()
    constraints: tuple[str, ...] = ()
    success_criteria: tuple[str, ...] = ()
    decide_later_items: tuple[str, ...] = ()
    """Items deferred or identified as premature during the PM interview.

    Includes both feature-level deferrals and questions that were premature
    or unknowable. Stored as the original question/item text so they can be
    surfaced later when enough context exists to address them.
    """
    assumptions: tuple[str, ...] = ()
    interview_id: str = ""
    codebase_context: str = ""
    brownfield_repos: tuple[dict[str, str], ...] = ()
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(),
    )

    # ── Deprecated fields (backward compat, merged via __post_init__) ──
    deferred_items: tuple[str, ...] = ()
    deferred_decisions: tuple[str, ...] = ()
    seed: Any = ""
    """Legacy dev seed reference. Accepts str, dict, or Seed-like objects.

    Preserved on round-trip: if the value has a ``to_dict()`` method it is
    serialized via that; dicts and strings are passed through as-is.
    """
    referenced_repos: tuple[dict[str, str], ...] = ()

    def __post_init__(self) -> None:
        """Merge deprecated fields into their canonical counterparts."""
        # Merge deferred_items and deferred_decisions → decide_later_items
        if self.deferred_items or self.deferred_decisions:
            combined = list(self.decide_later_items)
            for item in self.deferred_items:
                if item not in combined:
                    combined.append(item)
            for item in self.deferred_decisions:
                if item not in combined:
                    combined.append(item)
            object.__setattr__(self, "decide_later_items", tuple(combined))
            object.__setattr__(self, "deferred_items", ())
            object.__setattr__(self, "deferred_decisions", ())

        # Merge referenced_repos → brownfield_repos (additive, not replacement)
        if self.referenced_repos:
            if self.brownfield_repos:
                merged = list(self.brownfield_repos)
                existing_paths = {r.get("path") for r in merged}
                for r in self.referenced_repos:
                    if r.get("path") not in existing_paths:
                        merged.append(r)
                object.__setattr__(self, "brownfield_repos", tuple(merged))
            else:
                object.__setattr__(self, "brownfield_repos", self.referenced_repos)
            object.__setattr__(self, "referenced_repos", ())

    def to_dict(self) -> dict:
        """Convert to a plain dictionary for YAML serialization.

        Preserves legacy ``seed`` field when non-empty so that older
        PM seed artifacts survive a load/save round-trip without data loss.
        """
        d: dict = {
            "pm_id": self.pm_id,
            "product_name": self.product_name,
            "goal": self.goal,
            "user_stories": [
                {"persona": s.persona, "action": s.action, "benefit": s.benefit}
                for s in self.user_stories
            ],
            "constraints": list(self.constraints),
            "success_criteria": list(self.success_criteria),
            "decide_later_items": list(self.decide_later_items),
            "assumptions": list(self.assumptions),
            "interview_id": self.interview_id,
            "codebase_context": self.codebase_context,
            "brownfield_repos": [dict(r) for r in self.brownfield_repos],
            "created_at": self.created_at,
        }
        # Preserve legacy seed for round-trip safety.
        # Handles Seed objects (via to_dict()), raw dicts, and strings.
        # Explicit None check: falsey-but-present values ({}, 0) are preserved.
        if self.seed is not None and self.seed != "":
            if hasattr(self.seed, "to_dict"):
                d["seed"] = self.seed.to_dict()
            else:
                d["seed"] = self.seed
        return d

    @classmethod
    def from_dict(cls, data: dict) -> PMSeed:
        """Create a PMSeed from a dictionary (e.g., loaded from YAML).

        Handles backward compatibility: legacy fields ``deferred_items``,
        ``deferred_decisions``, ``seed``, and ``referenced_repos`` are
        accepted and migrated to their canonical counterparts via
        ``__post_init__``.
        """
        stories = tuple(
            UserStory(
                persona=s.get("persona", ""),
                action=s.get("action", ""),
                benefit=s.get("benefit", ""),
            )
            for s in data.get("user_stories", [])
        )

        # Backward compat: merge legacy deferred_items / deferred_decisions
        # into decide_later_items (canonical field since v0.25).
        decide_later = list(data.get("decide_later_items", []))
        for key in ("deferred_items", "deferred_decisions"):
            for item in data.get(key, []):
                if item not in decide_later:
                    decide_later.append(item)

        # Backward compat: merge referenced_repos into brownfield_repos (additive)
        brownfield_raw = list(data.get("brownfield_repos", []))
        for r in data.get("referenced_repos", []):
            if r not in brownfield_raw:
                brownfield_raw.append(r)

        # Rehydrate legacy seed: dict → Seed object if possible
        seed_raw = data.get("seed", "")
        if isinstance(seed_raw, dict):
            try:
                from ouroboros.core.seed import Seed as DevSeed

                seed_raw = DevSeed.from_dict(seed_raw)
            except Exception:
                pass  # Preserve as raw dict if Seed import/parse fails

        return cls(
            pm_id=data.get("pm_id") or f"pm_seed_{uuid4().hex[:12]}",
            product_name=data.get("product_name", ""),
            goal=data.get("goal", ""),
            user_stories=stories,
            constraints=tuple(data.get("constraints", [])),
            success_criteria=tuple(data.get("success_criteria", [])),
            decide_later_items=tuple(decide_later),
            assumptions=tuple(data.get("assumptions", [])),
            interview_id=data.get("interview_id", ""),
            codebase_context=data.get("codebase_context", ""),
            brownfield_repos=tuple(dict(r) for r in brownfield_raw),
            created_at=data.get("created_at", ""),
            seed=seed_raw,
        )

    def to_initial_context(self) -> str:
        """Serialize PMSeed to a string for dev interview handoff.

        This produces a YAML-formatted string suitable for passing as
        initial_context to a standard InterviewEngine session.
        """
        import yaml

        return yaml.dump(
            self.to_dict(),
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        )
