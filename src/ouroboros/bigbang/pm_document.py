"""PM document generator — produces human-readable pm.md from PMSeed.

Generates a Markdown PM document covering goals, constraints, user stories,
success criteria, and deferred items. Designed for PM readability.

Two generation modes:
- **Template-based** (``generate_pm_markdown``): deterministic, no LLM call.
- **LLM-based** (``PMDocumentGenerator``): uses the full Q&A transcript plus
  PMSeed to produce a richer, more readable document via LLM synthesis.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from ouroboros.bigbang.pm_seed import PMSeed
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

_DEFAULT_PM_DIR = ".ouroboros"
_PM_FILENAME = "pm.md"

_FALLBACK_MODEL = "claude-opus-4-6"

_PM_GENERATION_SYSTEM_PROMPT = """\
You are a Product Requirements Document (PM) writer. Given a full interview \
transcript and extracted product requirements, produce a polished, \
human-readable PM in Markdown format.

The PM MUST include the following sections (in order):
1. **Title** — the product/feature name as an H1 heading
2. **Goal** — a clear, concise product goal statement
3. **User Stories** — numbered user stories in "As a <persona>, I want to \
<action>, so that <benefit>" format
4. **Constraints** — bullet list of constraints (timeline, budget, compliance, \
technical limitations)
5. **Success Criteria** — numbered measurable success criteria
6. **Assumptions** — bullet list of assumptions made during requirements gathering
7. **Decide Later** — merged list of deferred items and premature/unknowable questions \
(include context on why deferred or why premature)

Omit any section that has no content (except Goal which should always appear).
Do NOT include a "Codebase Analysis" section — that is technical detail not relevant for PMs.

Guidelines:
- Write for a PM audience — avoid technical jargon unless necessary
- Be specific and actionable — avoid vague statements
- Preserve all information from the interview — do not invent requirements
- Use the interview conversation to add context and nuance beyond the \
structured seed data
- Keep the tone professional but accessible
- End with a footer showing the PM ID and interview ID

Output ONLY the Markdown document, no preamble or explanation.
"""


def generate_pm_markdown(seed: PMSeed) -> str:
    """Generate a human-readable PM Markdown document from a PMSeed.

    Args:
        seed: The PMSeed containing product requirements.

    Returns:
        Formatted Markdown string.
    """
    lines: list[str] = []

    # Title
    title = seed.product_name or "Product Requirements Document"
    lines.append(f"# {title}")
    lines.append("")
    if seed.created_at:
        lines.append(f"*Created At: {seed.created_at}*")
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
            lines.append(
                f"{i}. **As a** {story.persona}, **I want to** {story.action}, **so that** {story.benefit}."
            )
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

    # Decide Later
    all_decide_later = list(seed.decide_later_items or [])
    if all_decide_later:
        lines.append("## Decide Later")
        lines.append("")
        lines.append(
            "The following items were deferred or identified as premature at this stage. "
            "They should be revisited when more context is available:"
        )
        lines.append("")
        for item in all_decide_later:
            lines.append(f"- {item}")
        lines.append("")

    # Brownfield Context
    if seed.brownfield_repos:
        lines.append("## Existing Codebase Context")
        lines.append("")
        for repo in seed.brownfield_repos:
            name = repo.get("name") or repo.get("path") or "Unknown"
            desc = repo.get("desc", "")
            path = repo.get("path") or ""
            if path:
                lines.append(f"- **{name}** (`{path}`)")
            else:
                lines.append(f"- **{name}**")
            if desc:
                lines.append(f"  {desc}")
        lines.append("")

    # Footer
    footer_items: list[str] = []
    if seed.pm_id:
        footer_items.append(f"*PM ID: {seed.pm_id}*")
    if seed.interview_id:
        footer_items.append(f"*Interview ID: {seed.interview_id}*")
    if footer_items:
        lines.append("---")
        for item in footer_items:
            lines.append(item)
        lines.append("")

    return "\n".join(lines)


def save_pm_document(
    seed: PMSeed,
    output_dir: str | Path | None = None,
    *,
    output_path: str | Path | None = None,
) -> Path:
    """Generate and save a PM document.

    Args:
        seed: The PMSeed to generate the document from.
        output_dir: Directory to save pm.md in. Defaults to .ouroboros/.
            Ignored when *output_path* is provided.
        output_path: Full file path (directory + filename) for the PM
            document. When given, *output_dir* is ignored and the file
            is written to exactly this path.

    Returns:
        Path to the saved pm.md file.
    """
    if output_path is not None:
        pm_path = Path(output_path)
        pm_path.parent.mkdir(parents=True, exist_ok=True)
    elif output_dir is None:
        output_dir = Path.cwd() / _DEFAULT_PM_DIR
        output_dir.mkdir(parents=True, exist_ok=True)
        pm_path = output_dir / _PM_FILENAME
    else:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        pm_path = output_dir / _PM_FILENAME

    content = generate_pm_markdown(seed)
    pm_path.write_text(content, encoding="utf-8")

    log.info(
        "pm.document_saved",
        path=str(pm_path),
        product_name=seed.product_name,
    )

    return pm_path


# ──────────────────────────────────────────────────────────────────
# LLM-based PM Document Generator
# ──────────────────────────────────────────────────────────────────


@dataclass
class PMDocumentGenerator:
    """Generates a polished PM document using LLM from Q&A transcript + PMSeed.

    Unlike the template-based ``generate_pm_markdown``, this generator uses
    an LLM to synthesize a richer document that incorporates the full interview
    context — adding nuance, rationale, and connections that a template cannot.

    Falls back to the template-based generator if the LLM call fails.

    Attributes:
        llm_adapter: LLM adapter for document generation.
        model: Model identifier for the LLM call.

    Example:
        generator = PMDocumentGenerator(llm_adapter=adapter)
        result = await generator.generate(seed, qa_pairs)
        if result.is_ok:
            pm_path = generator.save(result.value, seed)
    """

    llm_adapter: LLMAdapter
    model: str = _FALLBACK_MODEL

    async def generate(
        self,
        seed: PMSeed,
        qa_pairs: list[tuple[str, str]] | None = None,
        interview_state: InterviewState | None = None,
    ) -> Result[str, ProviderError]:
        """Generate a PM Markdown document from PMSeed and Q&A history.

        Uses the LLM to produce a polished, readable PM incorporating both
        the structured seed data and the raw interview conversation. Falls
        back to template-based generation on LLM failure.

        Provide Q&A either as ``qa_pairs`` (list of (question, answer) tuples)
        or ``interview_state`` (from which rounds are extracted). If both are
        provided, ``qa_pairs`` takes precedence.

        Args:
            seed: The PMSeed containing extracted requirements.
            qa_pairs: Optional list of (question, answer) tuples from interview.
            interview_state: Optional InterviewState to extract Q&A from.

        Returns:
            Result containing the generated Markdown string or ProviderError.
        """
        # Extract Q&A from interview state if not provided directly
        if qa_pairs is None and interview_state is not None:
            qa_pairs = [(r.question, r.user_response or "") for r in interview_state.rounds]

        user_prompt = self._build_generation_prompt(seed, qa_pairs)

        messages = [
            Message(role=MessageRole.SYSTEM, content=_PM_GENERATION_SYSTEM_PROMPT),
            Message(role=MessageRole.USER, content=user_prompt),
        ]

        config = CompletionConfig(
            model=self.model,
            temperature=0.3,
            max_tokens=8192,
        )

        log.info(
            "pm.document_generation_started",
            product_name=seed.product_name,
            qa_count=len(qa_pairs) if qa_pairs else 0,
        )

        result = await self.llm_adapter.complete(messages, config)

        if result.is_err:
            log.warning(
                "pm.document_llm_failed",
                error=str(result.error),
                fallback="template",
            )
            # Fall back to template-based generation
            return Result.ok(generate_pm_markdown(seed))

        content = result.value.content.strip()

        # Validate that the LLM produced something reasonable
        if not content or len(content) < 50:
            log.warning(
                "pm.document_llm_too_short",
                content_length=len(content),
                fallback="template",
            )
            return Result.ok(generate_pm_markdown(seed))

        log.info(
            "pm.document_generated",
            product_name=seed.product_name,
            content_length=len(content),
        )

        return Result.ok(content)

    def save(
        self,
        content: str,
        seed: PMSeed,
        output_dir: str | Path | None = None,
    ) -> Path:
        """Save generated PM content to pm.md.

        Args:
            content: The generated PM Markdown content.
            seed: The PMSeed (used for logging metadata).
            output_dir: Directory to save pm.md in. Defaults to .ouroboros/.

        Returns:
            Path to the saved pm.md file.
        """
        if output_dir is None:
            output_dir = Path.cwd() / _DEFAULT_PM_DIR
        else:
            output_dir = Path(output_dir)

        output_dir.mkdir(parents=True, exist_ok=True)
        pm_path = output_dir / _PM_FILENAME

        pm_path.write_text(content, encoding="utf-8")

        log.info(
            "pm.document_saved",
            path=str(pm_path),
            product_name=seed.product_name,
            pm_id=seed.pm_id,
        )

        return pm_path

    async def generate_and_save(
        self,
        seed: PMSeed,
        qa_pairs: list[tuple[str, str]] | None = None,
        interview_state: InterviewState | None = None,
        output_dir: str | Path | None = None,
    ) -> Result[Path, ProviderError]:
        """Generate and save PM document in one step.

        Convenience method that combines :meth:`generate` and :meth:`save`.

        Args:
            seed: The PMSeed containing extracted requirements.
            qa_pairs: Optional list of (question, answer) tuples.
            interview_state: Optional InterviewState to extract Q&A from.
            output_dir: Directory to save pm.md in. Defaults to .ouroboros/.

        Returns:
            Result containing path to saved pm.md or ProviderError.
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
        seed: PMSeed,
        qa_pairs: list[tuple[str, str]] | None = None,
    ) -> str:
        """Build the user prompt for PM generation.

        Combines the structured PMSeed data with the raw Q&A transcript
        to give the LLM full context for producing a rich document.

        Args:
            seed: The PMSeed with extracted requirements.
            qa_pairs: Optional Q&A pairs from the interview.

        Returns:
            Formatted prompt string.
        """
        parts: list[str] = []

        # Structured seed data
        parts.append("## Extracted Requirements (PMSeed)")
        parts.append("")
        parts.append(f"**Product Name:** {seed.product_name or 'Unnamed'}")
        if seed.pm_id:
            parts.append(f"**PM ID:** {seed.pm_id}")
        if seed.interview_id:
            parts.append(f"**Interview ID:** {seed.interview_id}")
        parts.append(f"**Goal:** {seed.goal or 'Not specified'}")
        parts.append("")

        if seed.user_stories:
            parts.append("**User Stories:**")
            for story in seed.user_stories:
                parts.append(
                    f"- As a {story.persona}, I want to {story.action}, so that {story.benefit}."
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

        all_decide_later = list(seed.decide_later_items or [])
        if all_decide_later:
            parts.append("**Decide Later:**")
            for item in all_decide_later:
                parts.append(f"- {item}")
            parts.append("")

        if seed.brownfield_repos:
            parts.append("**Brownfield Repositories:**")
            for repo in seed.brownfield_repos:
                name = repo.get("name") or repo.get("path") or "Unknown"
                path = repo.get("path") or ""
                desc = repo.get("desc", "")
                label = f"{name} ({path})" if path else name
                parts.append(f"- {label}{f' — {desc}' if desc else ''}")
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
            "Generate a polished PM document from the above requirements and interview transcript."
        )

        return "\n".join(parts)
