"""Magic keyword detection for skill routing.

This module provides:
- Magic keyword detection in user messages
- Priority-based matching (specific > general)
- Prefix detection (e.g., "ooo", "ouroboros:")
- Pattern-based routing to skills
- Fallback handling for no matches
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

import structlog

from ouroboros.plugin.skills.registry import SkillMetadata, SkillRegistry, get_registry

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

    # Common magic prefix patterns
    PREFIX_PATTERNS = [
        r"^/?(ouroboros|ooo):(\w+)",  # /ouroboros:run, ooo:interview
        r"^(\w+)\s+(ouroboros|ooo)\s+(\w+)",  # "please ouroboros run"
        r"^(?:/?)?ooo\s+(\w+)",  # "ooo run", "ooo interview" (ooo without colon)
    ]

    def __init__(self, registry: SkillRegistry | None = None) -> None:
        """Initialize the keyword detector.

        Args:
            registry: Optional skill registry. Uses global singleton if not provided.
        """
        self._registry = registry or get_registry()
        self._compiled_patterns = [
            re.compile(pattern, re.IGNORECASE)
            for pattern in self.PREFIX_PATTERNS
        ]

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

        # Try each compiled pattern
        for pattern in self._compiled_patterns:
            for match in pattern.finditer(user_input):
                groups = match.groups()
                # Extract skill name from match
                skill_name = None
                for group in groups:
                    if group and group.isalpha():
                        # Check if this is a registered skill
                        if self._registry.get_skill(group):
                            skill_name = group
                            break

                if skill_name:
                    skill = self._registry.get_skill(skill_name)
                    if skill:
                        matches.append(KeywordMatch(
                            skill_name=skill_name,
                            match_type=MatchType.EXACT_PREFIX,
                            matched_text=match.group(0),
                            confidence=1.0,  # Exact prefix = highest confidence
                            metadata={"pattern": pattern.pattern},
                        ))

        # Check for "ooo" bare command (welcome skill)
        if user_input.strip().lower() in ("ooo", "/ouroboros", "ouroboros"):
            welcome_skill = self._registry.get_skill("welcome")
            if welcome_skill:
                matches.append(KeywordMatch(
                    skill_name="welcome",
                    match_type=MatchType.EXACT_PREFIX,
                    matched_text=user_input.strip(),
                    confidence=1.0,
                ))

        return matches

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

                    matches.append(KeywordMatch(
                        skill_name=skill_name,
                        match_type=MatchType.TRIGGER_KEYWORD,
                        matched_text=keyword,
                        confidence=confidence,
                        metadata={"keyword": keyword},
                    ))

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


def is_magic_command(user_input: str) -> bool:
    """Check if user input is a magic command.

    Args:
        user_input: The user's input text.

    Returns:
        True if input appears to be a magic command.
    """
    # Quick check for common patterns
    input_lower = user_input.strip().lower()
    magic_indicators = [
        "ooo:",
        "/ouroboros:",
        "ouroboros:",
        "ooo ",  # "ooo run"
    ]

    return any(indicator in input_lower for indicator in magic_indicators)
