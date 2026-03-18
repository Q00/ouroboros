"""PRD document generator — produces human-readable prd.md from PRDSeed.

Generates a Markdown PRD document covering goals, constraints, user stories,
success criteria, and deferred items. Designed for PM readability.

Two generation modes:
- **Template-based** (``generate_prd_markdown``): deterministic, no LLM call.
- **LLM-based** (``PRDDocumentGenerator``): uses the full Q&A transcript plus
  PRDSeed to produce a richer, more readable document via LLM synthesis.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from ouroboros.bigbang.prd_seed import PRDSeed
from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result
from ouroboros.providers.base import (
    CompletionConfig,
    LLMAdapter,
    Message,
    MessageRole,
)

if TYPE_CHECKING:
    from ouroboros.bigbang.interview import InterviewState

log = structlog.get_logger()

_DEFAULT_PRD_DIR = ".ouroboros"
_PRD_FILENAME = "prd.md"

_FALLBACK_MODEL = "claude-opus-4-6"

_PRD_GENERATION_SYSTEM_PROMPT = """\
You are a Product Requirements Document (PRD) writer. Given a full interview \
transcript and extracted product requirements, produce a polished, \
human-readable PRD in Markdown format.

The PRD MUST include the following sections (in order):
1. **Title** — the product/feature name as an H1 heading
2. **Goal** — a clear, concise product goal statement
3. **User Stories** — numbered user stories in "As a <persona>, I want to \
<action>, so that <benefit>" format
4. **Constraints** — bullet list of constraints (timeline, budget, compliance, \
technical limitations)
5. **Success Criteria** — numbered measurable success criteria
6. **Assumptions** — bullet list of assumptions made during requirements gathering
7. **Deferred Items** — items explicitly deferred to later phases \
(include context on why deferred)
8. **Decide Later** — questions that are premature or unknowable at this stage
9. **Existing Codebase Context** — if brownfield repos are referenced

Omit any section that has no content (except Goal which should always appear).

Guidelines:
- Write for a PM audience — avoid technical jargon unless necessary
- Be specific and actionable — avoid vague statements
- Preserve all information from the interview — do not invent requirements
- Use the interview conversation to add context and nuance beyond the \
structured seed data
- Keep the tone professional but accessible
- End with a footer showing the PRD ID and interview ID

Output ONLY the Markdown document, no preamble or explanation.
"""


def generate_prd_markdown(seed: PRDSeed) -> str:
    """Generate a human-readable PRD Markdown document from a PRDSeed.

    Args:
        seed: The PRDSeed containing product requirements.

    Returns:
        Formatted Markdown string.
    """
    lines: list[str] = []

    # Title
    title = seed.product_name or "Product Requirements Document"
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"*Generated: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}*")
    lines.append(f"*PRD ID: {seed.prd_id}*")
    lines.append("")

    # Goal
    lines.append("## Goal")
    lines.append("")
    lines.append(seed.goal or "*No goal specified.*")
    lines.append("")

    # User Stories
    if seed.user_stories:
        lines.append("## User Stories")
        lines.append("")
        for i, story in enumerate(seed.user_stories, 1):
            lines.append(f"{i}. **As a** {story.persona}, **I want to** {story.action}, **so that** {story.benefit}.")
        lines.append("")

    # Constraints
    if seed.constraints:
        lines.append("## Constraints")
        lines.append("")
        for constraint in seed.constraints:
            lines.append(f"- {constraint}")
        lines.append("")

    # Success Criteria
    if seed.success_criteria:
        lines.append("## Success Criteria")
        lines.append("")
        for i, criterion in enumerate(seed.success_criteria, 1):
            lines.append(f"{i}. {criterion}")
        lines.append("")

    # Assumptions
    if seed.assumptions:
        lines.append("## Assumptions")
        lines.append("")
        for assumption in seed.assumptions:
            lines.append(f"- {assumption}")
        lines.append("")

    # Deferred Items
    if seed.deferred_items:
        lines.append("## Deferred Items")
        lines.append("")
        lines.append("The following items were identified during requirements gathering but deferred to later phases:")
        lines.append("")
        for item in seed.deferred_items:
            lines.append(f"- {item}")
        lines.append("")

    # Decide-Later Items
    if seed.decide_later_items:
        lines.append("## Decide Later")
        lines.append("")
        lines.append("The following questions were identified as premature or unknowable at this stage. They should be revisited when more context is available:")
        lines.append("")
        for item in seed.decide_later_items:
            lines.append(f"- {item}")
        lines.append("")

    # Brownfield Context
    if seed.brownfield_repos:
        lines.append("## Existing Codebase Context")
        lines.append("")
        for repo in seed.brownfield_repos:
            name = repo.get("name", repo.get("path", "Unknown"))
            desc = repo.get("desc", "")
            path = repo.get("path", "")
            lines.append(f"- **{name}** (`{path}`)")
            if desc:
                lines.append(f"  {desc}")
        lines.append("")

    if seed.codebase_context:
        lines.append("### Codebase Analysis")
        lines.append("")
        lines.append(seed.codebase_context)
        lines.append("")

    # Footer
    lines.append("---")
    lines.append(f"*Interview ID: {seed.interview_id}*")
    lines.append("")

    return "\n".join(lines)


def save_prd_document(
    seed: PRDSeed,
    output_dir: str | Path | None = None,
) -> Path:
    """Generate and save a PRD document.

    Args:
        seed: The PRDSeed to generate the document from.
        output_dir: Directory to save prd.md in. Defaults to .ouroboros/.

    Returns:
        Path to the saved prd.md file.
    """
    if output_dir is None:
        output_dir = Path.cwd() / _DEFAULT_PRD_DIR
    else:
        output_dir = Path(output_dir)

    output_dir.mkdir(parents=True, exist_ok=True)
    prd_path = output_dir / _PRD_FILENAME

    content = generate_prd_markdown(seed)
    prd_path.write_text(content, encoding="utf-8")

    log.info(
        "prd.document_saved",
        path=str(prd_path),
        product_name=seed.product_name,
    )

    return prd_path


# ──────────────────────────────────────────────────────────────────
# LLM-based PRD Document Generator
# ──────────────────────────────────────────────────────────────────


@dataclass
class PRDDocumentGenerator:
    """Generates a polished PRD document using LLM from Q&A transcript + PRDSeed.

    Unlike the template-based ``generate_prd_markdown``, this generator uses
    an LLM to synthesize a richer document that incorporates the full interview
    context — adding nuance, rationale, and connections that a template cannot.

    Falls back to the template-based generator if the LLM call fails.

    Attributes:
        llm_adapter: LLM adapter for document generation.
        model: Model identifier for the LLM call.

    Example:
        generator = PRDDocumentGenerator(llm_adapter=adapter)
        result = await generator.generate(seed, qa_pairs)
        if result.is_ok:
            prd_path = generator.save(result.value, seed)
    """

    llm_adapter: LLMAdapter
    model: str = _FALLBACK_MODEL

    async def generate(
        self,
        seed: PRDSeed,
        qa_pairs: list[tuple[str, str]] | None = None,
        interview_state: InterviewState | None = None,
    ) -> Result[str, ProviderError]:
        """Generate a PRD Markdown document from PRDSeed and Q&A history.

        Uses the LLM to produce a polished, readable PRD incorporating both
        the structured seed data and the raw interview conversation. Falls
        back to template-based generation on LLM failure.

        Provide Q&A either as ``qa_pairs`` (list of (question, answer) tuples)
        or ``interview_state`` (from which rounds are extracted). If both are
        provided, ``qa_pairs`` takes precedence.

        Args:
            seed: The PRDSeed containing extracted requirements.
            qa_pairs: Optional list of (question, answer) tuples from interview.
            interview_state: Optional InterviewState to extract Q&A from.

        Returns:
            Result containing the generated Markdown string or ProviderError.
        """
        # Extract Q&A from interview state if not provided directly
        if qa_pairs is None and interview_state is not None:
            qa_pairs = [
                (r.question, r.user_response or "")
                for r in interview_state.rounds
            ]

        user_prompt = self._build_generation_prompt(seed, qa_pairs)

        messages = [
            Message(role=MessageRole.SYSTEM, content=_PRD_GENERATION_SYSTEM_PROMPT),
            Message(role=MessageRole.USER, content=user_prompt),
        ]

        config = CompletionConfig(
            model=self.model,
            temperature=0.3,
            max_tokens=8192,
        )

        log.info(
            "prd.document_generation_started",
            product_name=seed.product_name,
            qa_count=len(qa_pairs) if qa_pairs else 0,
        )

        result = await self.llm_adapter.complete(messages, config)

        if result.is_err:
            log.warning(
                "prd.document_llm_failed",
                error=str(result.error),
                fallback="template",
            )
            # Fall back to template-based generation
            return Result.ok(generate_prd_markdown(seed))

        content = result.value.content.strip()

        # Validate that the LLM produced something reasonable
        if not content or len(content) < 50:
            log.warning(
                "prd.document_llm_too_short",
                content_length=len(content),
                fallback="template",
            )
            return Result.ok(generate_prd_markdown(seed))

        log.info(
            "prd.document_generated",
            product_name=seed.product_name,
            content_length=len(content),
        )

        return Result.ok(content)

    def save(
        self,
        content: str,
        seed: PRDSeed,
        output_dir: str | Path | None = None,
    ) -> Path:
        """Save generated PRD content to prd.md.

        Args:
            content: The generated PRD Markdown content.
            seed: The PRDSeed (used for logging metadata).
            output_dir: Directory to save prd.md in. Defaults to .ouroboros/.

        Returns:
            Path to the saved prd.md file.
        """
        if output_dir is None:
            output_dir = Path.cwd() / _DEFAULT_PRD_DIR
        else:
            output_dir = Path(output_dir)

        output_dir.mkdir(parents=True, exist_ok=True)
        prd_path = output_dir / _PRD_FILENAME

        prd_path.write_text(content, encoding="utf-8")

        log.info(
            "prd.document_saved",
            path=str(prd_path),
            product_name=seed.product_name,
            prd_id=seed.prd_id,
        )

        return prd_path

    async def generate_and_save(
        self,
        seed: PRDSeed,
        qa_pairs: list[tuple[str, str]] | None = None,
        interview_state: InterviewState | None = None,
        output_dir: str | Path | None = None,
    ) -> Result[Path, ProviderError]:
        """Generate and save PRD document in one step.

        Convenience method that combines :meth:`generate` and :meth:`save`.

        Args:
            seed: The PRDSeed containing extracted requirements.
            qa_pairs: Optional list of (question, answer) tuples.
            interview_state: Optional InterviewState to extract Q&A from.
            output_dir: Directory to save prd.md in. Defaults to .ouroboros/.

        Returns:
            Result containing path to saved prd.md or ProviderError.
        """
        result = await self.generate(
            seed,
            qa_pairs=qa_pairs,
            interview_state=interview_state,
        )

        if result.is_err:
            return Result.err(result.error)

        path = self.save(result.value, seed, output_dir)
        return Result.ok(path)

    # ──────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _build_generation_prompt(
        seed: PRDSeed,
        qa_pairs: list[tuple[str, str]] | None = None,
    ) -> str:
        """Build the user prompt for PRD generation.

        Combines the structured PRDSeed data with the raw Q&A transcript
        to give the LLM full context for producing a rich document.

        Args:
            seed: The PRDSeed with extracted requirements.
            qa_pairs: Optional Q&A pairs from the interview.

        Returns:
            Formatted prompt string.
        """
        parts: list[str] = []

        # Structured seed data
        parts.append("## Extracted Requirements (PRDSeed)")
        parts.append("")
        parts.append(f"**Product Name:** {seed.product_name or 'Unnamed'}")
        parts.append(f"**PRD ID:** {seed.prd_id}")
        parts.append(f"**Interview ID:** {seed.interview_id}")
        parts.append(f"**Goal:** {seed.goal or 'Not specified'}")
        parts.append("")

        if seed.user_stories:
            parts.append("**User Stories:**")
            for story in seed.user_stories:
                parts.append(
                    f"- As a {story.persona}, I want to {story.action}, "
                    f"so that {story.benefit}."
                )
            parts.append("")

        if seed.constraints:
            parts.append("**Constraints:**")
            for c in seed.constraints:
                parts.append(f"- {c}")
            parts.append("")

        if seed.success_criteria:
            parts.append("**Success Criteria:**")
            for sc in seed.success_criteria:
                parts.append(f"- {sc}")
            parts.append("")

        if seed.assumptions:
            parts.append("**Assumptions:**")
            for a in seed.assumptions:
                parts.append(f"- {a}")
            parts.append("")

        if seed.deferred_items:
            parts.append("**Deferred Items:**")
            for d in seed.deferred_items:
                parts.append(f"- {d}")
            parts.append("")

        if seed.decide_later_items:
            parts.append("**Decide Later Items:**")
            for dl in seed.decide_later_items:
                parts.append(f"- {dl}")
            parts.append("")

        if seed.brownfield_repos:
            parts.append("**Brownfield Repositories:**")
            for repo in seed.brownfield_repos:
                name = repo.get("name", "Unknown")
                path = repo.get("path", "")
                desc = repo.get("desc", "")
                parts.append(f"- {name} ({path}){f' — {desc}' if desc else ''}")
            parts.append("")

        if seed.codebase_context:
            parts.append("**Codebase Analysis:**")
            parts.append(seed.codebase_context[:3000])
            parts.append("")

        # Q&A transcript
        if qa_pairs:
            parts.append("## Full Interview Transcript")
            parts.append("")
            for i, (question, answer) in enumerate(qa_pairs, 1):
                parts.append(f"**Q{i}:** {question}")
                parts.append(f"**A{i}:** {answer}")
                parts.append("")

        parts.append(
            "Generate a polished PRD document from the above requirements "
            "and interview transcript."
        )

        return "\n".join(parts)
