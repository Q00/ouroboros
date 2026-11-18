"""The language of the answer the console drafts for "Suggest an answer" (#3050).

Models drift into another language when a prompt only implies one, so the
console names it. ``goal_language`` reads it from the chat goal's script,
without a model: Hangul is Korean, kana Japanese, Han Chinese, Cyrillic
Russian, and anything else English, since a check this cheap cannot tell
Latin-script languages apart. ``in_language`` checks a reply's script against
it, and ``plain_answer`` turns line breaks, escaped or real, into spaces.

The detection order and script ranges match ``ooo auto``'s answer check in
PR #3053; once that lands, this module can defer to it.
"""

from __future__ import annotations

from collections.abc import Mapping
import re

LANGUAGE_NAMES: Mapping[str, str] = {
    "en": "English",
    "ko": "Korean",
    "ja": "Japanese",
    "zh": "Chinese",
    "ru": "Russian",
}

_SCRIPTS: Mapping[str, re.Pattern[str]] = {
    "hangul": re.compile(r"[ᄀ-ᇿ㄰-㆏가-힣]"),
    "kana": re.compile(r"[぀-ヿ]"),
    "han": re.compile(r"[一-鿿]"),
    "cyrillic": re.compile(r"[Ѐ-ӿ]"),
    "latin": re.compile(r"[A-Za-z]"),
}
# The first script found decides. Japanese mixes kana with Han, so kana is checked first.
_DETECTION_ORDER: tuple[tuple[str, str], ...] = (
    ("ko", "hangul"),
    ("ja", "kana"),
    ("zh", "han"),
    ("ru", "cyrillic"),
)
# What a reply in each language must contain, and what it must not.
_REQUIRED_SCRIPT: Mapping[str, str] = {
    "en": "latin",
    "ko": "hangul",
    "ja": "kana",
    "zh": "han",
    "ru": "cyrillic",
}
_FORBIDDEN_SCRIPTS: Mapping[str, tuple[str, ...]] = {
    "en": ("hangul", "kana", "han", "cyrillic"),
    "ko": ("kana", "han", "cyrillic"),
    "ja": ("hangul", "cyrillic"),
    "zh": ("hangul", "kana", "cyrillic"),
    "ru": ("hangul", "kana", "han"),
}
# Identifiers and commands in inline code may use any script.
_INLINE_CODE = re.compile(r"`[^`]*`")
_ESCAPED_BREAK = re.compile(r"\\[nrt]")


def goal_language(goal: str) -> str:
    """``"ko"``, ``"ja"``, ``"zh"``, ``"ru"`` or ``"en"``: the language ``goal`` is written in."""
    for language, script in _DETECTION_ORDER:
        if _SCRIPTS[script].search(goal):
            return language
    return "en"


def plain_answer(text: str) -> str:
    """``text`` on one line: escaped (a backslash and ``n``) and real line breaks become spaces."""
    return " ".join(_ESCAPED_BREAK.sub(" ", text).split())


def in_language(text: str, language: str) -> bool:
    """Whether ``text`` is written in ``language``'s script and no other, outside inline code."""
    prose = _INLINE_CODE.sub(" ", text)
    if not _SCRIPTS[_REQUIRED_SCRIPT[language]].search(prose):
        return False
    return not any(_SCRIPTS[script].search(prose) for script in _FORBIDDEN_SCRIPTS[language])


__all__ = ["LANGUAGE_NAMES", "goal_language", "in_language", "plain_answer"]
