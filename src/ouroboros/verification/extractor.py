"""AssertionExtractor — converts ACs into verifiable SpecAssertions.

Uses an LLM to classify each AC into a verification tier and extract
machine-checkable patterns (regex, file paths, expected values).

Results are cached by seed_id to avoid redundant LLM calls across
generations that share the same ACs.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import json
import logging

from ouroboros.config import get_assertion_extraction_model
from ouroboros.core.types import Result
from ouroboros.providers.base import (
    CompletionConfig,
    LLMAdapter,
    Message,
    MessageRole,
)
from ouroboros.verification.models import SpecAssertion, VerificationTier

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a spec verification assistant. Given acceptance criteria for a software project, extract machine-verifiable assertions.

For each AC, classify it into a verification tier:
- t1_constant: Contains specific values, numbers, config settings that can be found via regex in source code.
  Examples: "WARMUP_FRAMES=10", "timeout of 30 seconds", "maximum 5 retries"
- t2_structural: Requires specific files, classes, interfaces, or functions to exist.
  Examples: "CameraProvider interface", "tests directory", "CLI accepts --verbose flag"
- t3_behavioral: Requires running code or tests to verify (test output analysis).
  Examples: "3 calls return median score", "handles errors gracefully"
- t4_unverifiable: Subjective or requires human judgment.
  Examples: "UX feels natural", "code is clean"

Respond with a JSON array. Each element:
{
    "ac_index": 0,
    "tier": "t1_constant",
    "pattern": "WARMUP_FRAMES\\s*=\\s*",
    "expected_value": "10",
    "file_hint": "*.py",
    "description": "Warmup frames should be set to 10"
}

Rules:
- pattern: A regex pattern to search for in source files. For t1, include the variable/constant name. For t2, use file or class name pattern.
- expected_value: The expected value for t1 (the actual number/string). For t2, the expected name. Empty for t3/t4.
- file_hint: Glob pattern for files to search (e.g., "*.py", "src/**/*.ts", "config.*"). Empty if unknown.
- One AC may produce 0-3 assertions (e.g., an AC with multiple checkable values).
- For t3/t4, still include the entry but with empty pattern/expected_value.
- Be conservative: if unsure, classify as t3_behavioral rather than t1/t2.

Return ONLY the JSON array, no markdown fences."""


@dataclass
class AssertionExtractor:
    """Extracts verifiable assertions from acceptance criteria using LLM.

    Caches results by seed_id so extraction happens only once per seed,
    even across multiple evaluation cycles.
    """

    llm_adapter: LLMAdapter
    model: str = field(default_factory=get_assertion_extraction_model)
    max_cache_size: int = 64
    _cache: OrderedDict[str, tuple[SpecAssertion, ...]] = field(
        default_factory=OrderedDict, repr=False
    )

    async def extract(
        self,
        seed_id: str,
        acceptance_criteria: tuple[str, ...] | list[str],
    ) -> Result[tuple[SpecAssertion, ...], str]:
        """Extract assertions from ACs.

        Args:
            seed_id: Seed identifier for caching.
            acceptance_criteria: List of AC text strings.

        Returns:
            Result containing tuple of SpecAssertions or error string.
        """
        if seed_id in self._cache:
            logger.debug("AssertionExtractor cache hit: %s", seed_id)
            return Result.ok(self._cache[seed_id])

        if not acceptance_criteria:
            return Result.ok(())

        prompt = "Extract verifiable assertions from these acceptance criteria:\n\n"
        for i, ac in enumerate(acceptance_criteria):
            prompt += f"AC {i} (index {i}): {ac}\n"

        messages = [
            Message(role=MessageRole.SYSTEM, content=_SYSTEM_PROMPT),
            Message(role=MessageRole.USER, content=prompt),
        ]

        config = CompletionConfig(
            model=self.model,
            temperature=0.0,
            max_tokens=4096,
        )

        result = await self.llm_adapter.complete(messages, config)
        if result.is_err:
            logger.warning("AssertionExtractor LLM failed: %s", result.error)
            return Result.err(f"Extraction failed: {result.error}")

        assertions = self._parse_response(result.value.content, acceptance_criteria)
        self._cache[seed_id] = assertions
        # LRU eviction: remove oldest entry if cache exceeds max size
        while len(self._cache) > self.max_cache_size:
            self._cache.popitem(last=False)
        return Result.ok(assertions)

    def _parse_response(
        self,
        content: str,
        acceptance_criteria: tuple[str, ...] | list[str],
    ) -> tuple[SpecAssertion, ...]:
        """Parse LLM response into SpecAssertions."""
        try:
            cleaned = content.strip()
            if cleaned.startswith("```"):
                lines = cleaned.split("\n")
                cleaned = "\n".join(lines[1:-1])

            data = json.loads(cleaned)
            if not isinstance(data, list):
                logger.warning("Expected JSON array, got: %s", type(data))
                return ()

            assertions: list[SpecAssertion] = []
            for item in data:
                ac_idx = item.get("ac_index", 0)
                ac_text = acceptance_criteria[ac_idx] if ac_idx < len(acceptance_criteria) else ""
                try:
                    tier = VerificationTier(item.get("tier", "t4_unverifiable"))
                except ValueError:
                    tier = VerificationTier.T4_UNVERIFIABLE

                assertions.append(
                    SpecAssertion(
                        ac_index=ac_idx,
                        ac_text=ac_text,
                        tier=tier,
                        pattern=item.get("pattern", ""),
                        expected_value=item.get("expected_value", ""),
                        file_hint=item.get("file_hint", ""),
                        description=item.get("description", ""),
                    )
                )

            return tuple(assertions)

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("Failed to parse extraction response: %s", e)
            return ()
