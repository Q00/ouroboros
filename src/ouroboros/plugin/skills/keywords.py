"""Magic keyword detection for skill routing.

This module provides:
- Magic keyword detection in user messages
- Priority-based matching (specific > general)
- Prefix detection (e.g., "ooo", "ouroboros:")
- Pattern-based routing to skills
- Fallback handling for no matches
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Any

import structlog

from ouroboros.plugin.skills.registry import SkillRegistry, get_registry

log = structlog.get_logger()


class MatchType(Enum):
    """Type of keyword match."""

    EXACT_PREFIX = "exact_prefix"  # Exact magic prefix match (highest priority)
    PARTIAL_PREFIX = "partial_prefix"  # Partial prefix match
    TRIGGER_KEYWORD = "trigger_keyword"  # Natural language trigger
    FALLBACK = "fallback"  # No match, use default


@dataclass
class KeywordMatch:
    """Result of keyword detection.

    Attributes:
        skill_name: Name of the matched skill.
        match_type: Type of match that occurred.
        matched_text: The text that matched.
        confidence: Confidence score (0.0 to 1.0).
        metadata: Additional match metadata.
    """

    skill_name: str
    match_type: MatchType
    matched_text: str
    confidence: float
    metadata: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        """Validate confidence is in valid range."""
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("Confidence must be between 0.0 and 1.0")


class MagicKeywordDetector:
    """Detects magic keywords and routes to appropriate skills.

    The detector analyzes user input for:
    1. Magic prefixes (e.g., "ooo run", "/ouroboros:interview")
    2. Natural language triggers (e.g., "clarify requirements")
    3. Pattern-based matches

    Routing priority:
    1. Exact prefix matches (highest)
    2. Partial prefix matches
    3. Trigger keyword matches
    4. No match (fallback)
    """

    def __init__(self, registry: SkillRegistry | None = None) -> None:
        """Initialize the keyword detector.

        Args:
            registry: Optional skill registry. Uses global singleton if not provided.
        """
        self._registry = registry or get_registry()

    def detect(self, user_input: str) -> list[KeywordMatch]:
        """Detect magic keywords in user input.

        Args:
            user_input: The user's input text.

        Returns:
            List of keyword matches, sorted by confidence (highest first).
        """
        matches: list[KeywordMatch] = []

        # Check for exact prefix matches first
        prefix_matches = self._detect_prefixes(user_input)
        matches.extend(prefix_matches)

        # Check for trigger keyword matches
        if not prefix_matches:
            trigger_matches = self._detect_triggers(user_input)
            matches.extend(trigger_matches)

        # Sort by confidence (prefix matches have higher confidence)
        matches.sort(key=lambda m: m.confidence, reverse=True)

        return matches

    def detect_best(self, user_input: str) -> KeywordMatch | None:
        """Detect the single best matching skill.

        Args:
            user_input: The user's input text.

        Returns:
            The best match if found, None otherwise.
        """
        matches = self.detect(user_input)
        return matches[0] if matches else None

    def _detect_prefixes(self, user_input: str) -> list[KeywordMatch]:
        """Detect magic prefix matches in user input.

        Args:
            user_input: The user's input text.

        Returns:
            List of prefix matches.
        """
        matches: list[KeywordMatch] = []
        stripped_input = user_input.strip()
        if not stripped_input:
            return matches

        normalized_input = stripped_input.lower()
        for prefix, skill_name in self._iter_exact_prefix_variants():
            if not self._matches_exact_prefix(normalized_input, prefix):
                continue

            matches.append(
                KeywordMatch(
                    skill_name=skill_name,
                    match_type=MatchType.EXACT_PREFIX,
                    matched_text=stripped_input[: len(prefix)],
                    confidence=1.0,
                    metadata={"prefix": prefix},
                )
            )

        return matches

    def _iter_exact_prefix_variants(self) -> list[tuple[str, str]]:
        """Build the exact prefix variants that are eligible for intercept."""
        candidates: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()

        for skill_name, metadata in self._registry.get_all_metadata().items():
            prefixes = [prefix.strip() for prefix in metadata.magic_prefixes if prefix.strip()]
            prefixes.append(f"ooo {skill_name}")
            if skill_name == "welcome":
                prefixes.extend(("ooo", "/ouroboros", "ouroboros"))

            for prefix in prefixes:
                key = (prefix.lower(), skill_name)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append((prefix, skill_name))

        candidates.sort(key=lambda item: len(item[0]), reverse=True)
        return candidates

    @staticmethod
    def _matches_exact_prefix(normalized_input: str, prefix: str) -> bool:
        """Check whether user input begins with an exact deterministic prefix."""
        normalized_prefix = prefix.lower()
        if normalized_input == normalized_prefix:
            return True

        if ":" not in normalized_prefix and " " not in normalized_prefix:
            return False

        if not normalized_input.startswith(normalized_prefix):
            return False

        if len(normalized_input) == len(normalized_prefix):
            return True

        return normalized_input[len(normalized_prefix)].isspace()

    def _detect_triggers(self, user_input: str) -> list[KeywordMatch]:
        """Detect trigger keyword matches in user input.

        Args:
            user_input: The user's input text.

        Returns:
            List of trigger matches.
        """
        matches: list[KeywordMatch] = []
        input_lower = user_input.lower()

        # Get all skills with trigger keywords
        all_metadata = self._registry.get_all_metadata()

        for skill_name, metadata in all_metadata.items():
            if not metadata.trigger_keywords:
                continue

            for keyword in metadata.trigger_keywords:
                keyword_lower = keyword.lower()
                if keyword_lower in input_lower:
                    # Calculate confidence based on match specificity
                    confidence = self._calculate_trigger_confidence(
                        keyword_lower,
                        input_lower,
                    )

                    matches.append(
                        KeywordMatch(
                            skill_name=skill_name,
                            match_type=MatchType.TRIGGER_KEYWORD,
                            matched_text=keyword,
                            confidence=confidence,
                            metadata={"keyword": keyword},
                        )
                    )

        return matches

    def _calculate_trigger_confidence(
        self,
        keyword: str,
        input_text: str,
    ) -> float:
        """Calculate confidence score for a trigger keyword match.

        Args:
            keyword: The matched keyword.
            input_text: The input text that matched.

        Returns:
            Confidence score between 0.0 and 1.0.
        """
        # Exact match = 1.0
        if keyword == input_text:
            return 1.0

        # Keyword at start = 0.9
        if input_text.startswith(keyword):
            return 0.9

        # Contains keyword with word boundary = 0.8
        pattern = r"\b" + re.escape(keyword) + r"\b"
        if re.search(pattern, input_text):
            return 0.8

        # Substring match = 0.6
        if keyword in input_text:
            return 0.6

        return 0.5


def detect_magic_keywords(
    user_input: str,
    registry: SkillRegistry | None = None,
) -> list[KeywordMatch]:
    """Convenience function to detect magic keywords.

    Args:
        user_input: The user's input text.
        registry: Optional skill registry.

    Returns:
        List of keyword matches, sorted by confidence.
    """
    detector = MagicKeywordDetector(registry)
    return detector.detect(user_input)


def route_to_skill(
    user_input: str,
    registry: SkillRegistry | None = None,
) -> tuple[str | None, MatchType]:
    """Route user input to the best matching skill.

    Args:
        user_input: The user's input text.
        registry: Optional skill registry.

    Returns:
        Tuple of (skill_name, match_type). Returns (None, MatchType.FALLBACK) if no match.
    """
    detector = MagicKeywordDetector(registry)
    match = detector.detect_best(user_input)

    if match:
        return match.skill_name, match.match_type

    return None, MatchType.FALLBACK


def is_magic_command(
    user_input: str,
    registry: SkillRegistry | None = None,
) -> bool:
    """Check if user input is a magic command.

    Args:
        user_input: The user's input text.
        registry: Optional skill registry used to validate exact prefixes.

    Returns:
        True if input appears to be a magic command.
    """
    stripped_input = user_input.strip()
    if not stripped_input:
        return False

    active_registry = registry or get_registry()
    if active_registry.get_all_metadata():
        detector = MagicKeywordDetector(active_registry)
        return bool(detector._detect_prefixes(stripped_input))

    if active_registry.skill_dir.exists():
        skill_names = sorted(
            skill_path.parent.name
            for skill_path in active_registry.skill_dir.glob("*/SKILL.md")
        )
        if skill_names:
            prefixes: list[str] = []
            for skill_name in skill_names:
                prefixes.extend(
                    [
                        f"ooo {skill_name}",
                        f"ooo:{skill_name}",
                        f"ouroboros:{skill_name}",
                        f"/ouroboros:{skill_name}",
                    ]
                )
                if skill_name == "welcome":
                    prefixes.extend(("ooo", "/ouroboros", "ouroboros"))

            normalized_input = stripped_input.lower()
            return any(
                MagicKeywordDetector._matches_exact_prefix(normalized_input, prefix)
                for prefix in prefixes
            )

    input_lower = stripped_input.lower()
    if input_lower in ("ooo", "/ouroboros", "ouroboros"):
        return True

    exact_patterns = (
        r"^ooo:[a-z0-9_-]+(?:\s+.*)?$",
        r"^ooo\s+[a-z0-9_-]+(?:\s+.*)?$",
        r"^(?:/ouroboros|ouroboros):[a-z0-9_-]+(?:\s+.*)?$",
    )
    return any(re.match(pattern, input_lower) for pattern in exact_patterns)
