"""The language and shape of the answers ``ooo auto`` writes in its interview.

``ooo auto`` answers interview questions on the user's behalf. The interviewer
scores those answers and the console shows them, so each one has to be in the
user's language, short, and in plain product words (#3050):

- ``answer_language`` names the language of the user's goal from its script,
  without a model: Hangul is Korean, kana Japanese, Han Chinese, Cyrillic
  Russian. Anything else is English, because a check this cheap cannot tell
  Latin-script languages apart. The goal decides, not the interviewer's
  question, so a drifting interviewer cannot carry the answers along.
- ``normalize_answer`` turns a model reply into one plain line: escaped line
  breaks (a literal backslash and ``n``) and real ones become spaces.
- ``answer_problems`` lists why a reply cannot be shown: no text in the expected
  script, another language's script, English words outside inline code in a
  non-Latin answer (``latin_prose``), more than ``MAX_ANSWER_PART_CHARS`` characters, or internal
  vocabulary (field names such as ``acceptance_criteria``). Text inside inline
  code is not checked for script, since identifiers and literals may use any.
- ``FALLBACK_ANSWERS`` holds one short, conservative answer per language, used
  when a model reply still has problems after one retry.
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
    "hangul": re.compile(r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7a3]"),
    "kana": re.compile(r"[\u3040-\u30ff]"),
    "han": re.compile(r"[\u4e00-\u9fff]"),
    "cyrillic": re.compile(r"[\u0400-\u04ff]"),
    "latin": re.compile(r"[A-Za-z]"),
}
# A goal naming any of these scripts is in that language, checked in order:
# Japanese mixes kana with Han, so kana is checked before Han.
_DETECTION_ORDER: tuple[tuple[str, str], ...] = (
    ("ko", "hangul"),
    ("ja", "kana"),
    ("zh", "han"),
    ("ru", "cyrillic"),
)
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
# Latin prose inside a non-Latin answer (``latin_prose``). A token is a run of
# Latin letters with the characters code and file names use; it starts at a
# letter, so a bare number is never one.
_LATIN_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_.'/+#-]*")
_TOKEN_TRAILING_PUNCTUATION = ".,;:!?'-"
_CODE_LITERALS = frozenset({"None", "True", "False", "null", "true", "false"})
_CAMEL_CASE = re.compile(r"[a-z][A-Z]")
_MAX_ACRONYM_LETTERS = 5
_INLINE_CODE = re.compile(r"`[^`]*`")
_ESCAPED_BREAK = re.compile(r"\\[nrt]")

# One answer part: the prompt asks for under 120 characters; past this the
# reply is rejected rather than cut.
MAX_ANSWER_PART_CHARS = 200

# Names of interview bookkeeping that mean nothing to the user.
_INTERNAL_TERMS = re.compile(
    r"\b(?:acceptance_criteria|verification_plan|failure_modes|runtime_context|non_goals"
    r"|open_questions|repo_facts|user_goal|conservative_default|ledger)\b"
    r"|`(?:goal|actors|inputs|outputs|constraints|non-goals)`"
    r"|\b(?:constraints?|acceptance|verification|inputs?|outputs?)\s+contracts?\b"
    r"|\bcommitted\s+contracts?\b|\[from-auto\]",
    re.IGNORECASE,
)
_INTERNAL_SEED_TERMS = re.compile(r"\b(?:the|this|a)\s+Seed\b|\bSeed\s+(?:sections?|fields?)\b")

FALLBACK_ANSWERS: Mapping[str, str] = {
    "en": (
        "Take the most conservative option: keep the change small and follow "
        "the project's existing conventions."
    ),
    "ko": "가장 보수적인 선택을 따른다: 변경을 작게 유지하고 프로젝트의 기존 관례를 따른다.",
    "ja": "最も保守的な選択をとる: 変更は小さく保ち、プロジェクトの既存の慣習に従う。",
    "zh": "采用最保守的做法：改动保持最小，并遵循项目现有的惯例。",
    "ru": (
        "Выбираем самый осторожный вариант: изменение минимальное, "
        "существующие соглашения проекта сохраняются."
    ),
}


def answer_language(text: str) -> str:
    """Return ``"ko"``, ``"ja"``, ``"zh"``, ``"ru"`` or ``"en"``: the language of ``text``.

    The first script found in :data:`_DETECTION_ORDER` decides; text with none of
    them is English. It picks the language auto answers in; it never routes a
    question.
    """
    for language, script in _DETECTION_ORDER:
        if _SCRIPTS[script].search(text):
            return language
    return "en"


def normalize_answer(text: str) -> str:
    """``text`` on one line: escaped and real line breaks become spaces."""
    return " ".join(_ESCAPED_BREAK.sub(" ", text).split())


def is_code_token(token: str) -> bool:
    """Whether a bare Latin token reads as code, a file name or a unit, not as a word.

    Code: a literal (``None``, ``True``, ``False``, ``null``, ``true``,
    ``false``); a token with a dot, slash, underscore, plus, hash or digit
    (``mode.py``, ``src/cli.py``, ``second_largest``, ``C++``, ``UTF-8``,
    ``v2``); a CamelCase name (``ValueError``, ``DataFrame``); an all-caps
    acronym of at most five letters (``CSV``, ``JSON``, ``HTTP``). Anything
    else is a word: ``pytest``, ``Return``, ``Python``, ``framework``.
    """
    if token in _CODE_LITERALS:
        return True
    if any(char in "._/+#" or char.isdigit() for char in token):
        return True
    if _CAMEL_CASE.search(token):
        return True
    return token.isupper() and len(token) <= _MAX_ACRONYM_LETTERS


def latin_prose(text: str) -> bool:
    """Whether ``text`` holds Latin prose: two Latin tokens in a row, one a word.

    Two tokens are in a row when nothing but spaces, digits or punctuation
    separates them; a letter of any other script starts a new row. A lone
    word is allowed (``pytest로``, ``Python 3.12에서``), and so is a row of
    code tokens (``mode.py config.toml``). A word next to anything Latin is
    prose: ``Return None``, ``hello world``, and also ``Visual Studio`` or
    ``pytest -q``, which belong in inline code. Strip inline code first.
    """
    row, row_has_word, previous_end = 0, False, -1
    for match in _LATIN_TOKEN.finditer(text):
        gap = text[previous_end : match.start()] if previous_end >= 0 else "x"
        if any(char.isalpha() for char in gap):
            row, row_has_word = 0, False
        token = match.group().rstrip(_TOKEN_TRAILING_PUNCTUATION)
        row += 1
        row_has_word = row_has_word or not is_code_token(token)
        if row >= 2 and row_has_word:
            return True
        previous_end = match.end()
    return False


def answer_problems(text: str, language: str, *, script_only: bool = False) -> list[str]:
    """Why ``text`` cannot be shown as an answer in ``language`` (empty when it can).

    ``script_only`` checks only the script, for text whose length and wording
    another check already owns.
    """
    problems: list[str] = []
    prose = _INLINE_CODE.sub(" ", text)
    required = _REQUIRED_SCRIPT.get(language, "latin")
    if not text.strip():
        return ["empty"]
    if not _SCRIPTS[required].search(prose):
        problems.append(f"not written in {LANGUAGE_NAMES.get(language, 'English')}")
    others = [s for s in _FORBIDDEN_SCRIPTS.get(language, ()) if _SCRIPTS[s].search(prose)]
    if others:
        problems.append(f"contains another language ({', '.join(others)} script)")
    if language != "en" and latin_prose(prose):
        problems.append("contains English words outside inline code")
    if script_only:
        return problems
    if len(text) > MAX_ANSWER_PART_CHARS:
        problems.append(f"longer than {MAX_ANSWER_PART_CHARS} characters")
    if _ESCAPED_BREAK.search(text) or "\n" in text:
        problems.append("contains line breaks")
    if _INTERNAL_TERMS.search(text) or _INTERNAL_SEED_TERMS.search(text):
        problems.append("uses internal field names")
    return problems


__all__ = [
    "FALLBACK_ANSWERS",
    "LANGUAGE_NAMES",
    "MAX_ANSWER_PART_CHARS",
    "answer_language",
    "answer_problems",
    "is_code_token",
    "latin_prose",
    "normalize_answer",
]
