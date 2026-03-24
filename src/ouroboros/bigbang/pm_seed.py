"""PM Seed — immutable specification for product requirements.

A PMSeed captures PM-level product requirements: goals, user stories,
constraints, success criteria, and deferred items. It is produced by the
PM interview flow and can be serialized to YAML for handoff to a
development interview via initial_context.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from ouroboros.core.seed import Seed


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
        deferred_items: Items explicitly deferred to later phases.
        assumptions: Assumptions made during the interview.
        interview_id: Reference to the source PM interview.
        codebase_context: Shared codebase exploration context (brownfield).
        brownfield_repos: Registered brownfield repositories.
        seed: Optional reference to the generated dev Seed.
        deferred_decisions: Decisions deferred during PM interview
            (architectural, technical, or strategic choices postponed).
        referenced_repos: Repos referenced during PM interview for context.
        created_at: When this seed was generated.
    """

    pm_id: str = field(default_factory=lambda: f"pm_seed_{uuid4().hex[:12]}")
    product_name: str = ""
    goal: str = ""
    user_stories: tuple[UserStory, ...] = ()
    constraints: tuple[str, ...] = ()
    success_criteria: tuple[str, ...] = ()
    deferred_items: tuple[str, ...] = ()
    decide_later_items: tuple[str, ...] = ()
    """Original question text for items classified as decide-later.

    These are questions that were premature or unknowable during the PM
    interview. Stored as the original question text so they can be surfaced
    later when enough context exists to answer them.
    """
    assumptions: tuple[str, ...] = ()
    interview_id: str = ""
    codebase_context: str = ""
    brownfield_repos: tuple[dict[str, str], ...] = ()
    seed: Seed | None = None
    """Optional reference to the generated dev Seed.

    Populated after the dev interview produces a Seed from the PM
    requirements. None while in the PM-only phase.
    """
    deferred_decisions: tuple[str, ...] = ()
    """Decisions deferred during the PM interview.

    These are architectural, technical, or strategic decisions that the PM
    chose to postpone. Distinct from decide_later_items (which are questions
    the classifier auto-deferred) and deferred_items (feature-level deferrals).
    """
    referenced_repos: tuple[dict[str, str], ...] = ()
    """Repos referenced during the PM interview for brownfield context.

    Each entry is a dict with keys: path, name, desc. This captures which
    repositories were consulted during codebase exploration, providing
    traceability for the PM requirements.
    """
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(),
    )

    def to_dict(self) -> dict:
        """Convert to a plain dictionary for YAML serialization."""
        return {
            "pm_id": self.pm_id,
            "product_name": self.product_name,
            "goal": self.goal,
            "user_stories": [
                {"persona": s.persona, "action": s.action, "benefit": s.benefit}
                for s in self.user_stories
            ],
            "constraints": list(self.constraints),
            "success_criteria": list(self.success_criteria),
            "deferred_items": list(self.deferred_items),
            "decide_later_items": list(self.decide_later_items),
            "assumptions": list(self.assumptions),
            "interview_id": self.interview_id,
            "codebase_context": self.codebase_context,
            "brownfield_repos": [dict(r) for r in self.brownfield_repos],
            "seed": self.seed.to_dict() if self.seed is not None else None,
            "deferred_decisions": list(self.deferred_decisions),
            "referenced_repos": [dict(r) for r in self.referenced_repos],
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> PMSeed:
        """Create a PMSeed from a dictionary (e.g., loaded from YAML)."""
        from ouroboros.core.seed import Seed

        stories = tuple(
            UserStory(
                persona=s.get("persona", ""),
                action=s.get("action", ""),
                benefit=s.get("benefit", ""),
            )
            for s in data.get("user_stories", [])
        )

        # Deserialize seed if present
        seed_data = data.get("seed")
        seed = Seed.from_dict(seed_data) if seed_data is not None else None

        return cls(
            pm_id=data.get("pm_id", ""),
            product_name=data.get("product_name", ""),
            goal=data.get("goal", ""),
            user_stories=stories,
            constraints=tuple(data.get("constraints", [])),
            success_criteria=tuple(data.get("success_criteria", [])),
            deferred_items=tuple(data.get("deferred_items", [])),
            decide_later_items=tuple(data.get("decide_later_items", [])),
            assumptions=tuple(data.get("assumptions", [])),
            interview_id=data.get("interview_id", ""),
            codebase_context=data.get("codebase_context", ""),
            brownfield_repos=tuple(dict(r) for r in data.get("brownfield_repos", [])),
            seed=seed,
            deferred_decisions=tuple(data.get("deferred_decisions", [])),
            referenced_repos=tuple(dict(r) for r in data.get("referenced_repos", [])),
            created_at=data.get("created_at", ""),
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
