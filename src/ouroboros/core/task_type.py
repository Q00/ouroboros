"""Task-type contracts shared by Seed authoring and repair."""

from __future__ import annotations

import re

SUPPORTED_TASK_TYPES = frozenset(
    {
        "analysis",
        "artifact",
        "code",
        "document",
        "documentation",
        "presentation",
        "research",
    }
)
_TASK_TYPE_PATTERN = (
    r"(?P<task_type>code|research|analysis|artifact|document|documentation|presentation)"
)
# ``SeedMetadata.parent_seed_id`` is intentionally an opaque non-empty string.
# Keep the bounded natural-language token parser compatible with punctuation
# already accepted by that schema instead of silently truncating lineage.
_SEED_ID_CHAR_PATTERN = r"A-Za-z0-9_:@/+~.=-"
# Parent IDs are opaque schema strings.  The natural-language boundary only
# excludes whitespace/clause punctuation; a final period is treated as sentence
# punctuation while internal punctuation and non-ASCII characters are kept.
_SEED_ID_PATTERN = r"[^\s,;!?]+?(?<!\.)"
_SEED_ID_CONTINUATION_PATTERN = r"[^\s,;!?.]|\.(?=[^\s,;!?])"
_TASK_TYPE_CONTRACT_PATTERNS = (
    re.compile(
        rf"\btask[_\s-]*type\b\s*(?:=|:)\s*{_TASK_TYPE_PATTERN}\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:set\s+(?:the\s+)?)?task[_\s-]*type\b\s+(?:is|equals?|to)\s+"
        rf"{_TASK_TYPE_PATTERN}\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\btask[_\s-]*type\b[^,;.!?\n]{{0,80}}?\b(?:must|should|needs?\s+to)\s+"
        rf"(?:be|remain|use|equal)\s+(?:an?\s+)?{_TASK_TYPE_PATTERN}\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\bkeep\s+(?:the\s+)?task[_\s-]*type\b\s+(?:as\s+)?{_TASK_TYPE_PATTERN}\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\buse\s+{_TASK_TYPE_PATTERN}\s+as\s+(?:the\s+)?task[_\s-]*type\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?<![A-Za-z0-9_])task[_\s-]*type\s*(?:은|는|이|가)?\s*"
        rf"{_TASK_TYPE_PATTERN}\s*(?:이어야|여야|로\s*(?:설정|지정))",
        re.IGNORECASE,
    ),
)

_NON_BINDING_CONTRACT_PATTERN = re.compile(
    r"\b(?:ignore|discard|disregard|superseded|obsolete|literal|discussed|phrase|"
    r"mention(?:s|ed|ing)?|compare(?:d|s|ing)?)\b"
    r"|\b(?:do(?:es)?|did|have|has|had)\s+not\b"
    r"|\b(?:don't|doesn't|doesn’t|didn't|didn’t|never|avoid(?:ed|ing)?|cannot|can't|can\s+not|without)\b"
    r"|\bdecided\s+not\s+to\b"
    r"|\b(?:am|is|are|was|were)\s+not\b"
    r"|\bnot\s+true\b"
    r"|\bnot\s+(?=(?:use\s+)?task[_\s-]*type\b|inherit\b|derive\b|parent\b)"
    r"|\b(?:must|should|may)\s+not\b"
    r"|\b(?:will|would)\s+not\b"
    r"|\b(?:won't|wouldn't|shouldn't|mustn't|isn't|aren't|wasn't|weren't)\b"
    r"|\b(?:won’t|wouldn’t|shouldn’t|mustn’t|isn’t|aren’t|wasn’t|weren’t)\b"
    r"|\b(?:am|is|are|was|were)\s+no\s+longer\b"
    r"|\bnot\s+allowed\b"
    r"|\b(?:declin(?:e|ed)|refus(?:e|ed))\s+to\b"
    r"|\b(?:rejected|abandoned)\s+"
    r"(?=(?:the\s+(?:plan|requirement|proposal)\s+to\s+)?"
    r"(?:(?:use|set|select)\s+)?(?:task[_\s-]*type\b|inherit\b|derive\b|parent\b))"
    r"|\bruled\s+out\b"
    r"|\bopted\s+not\s+to\b"
    r"|\bchose\s+not\s+to\b"
    r"|\bthere\s+(?:is|was)\s+no\s+(?:requirement|contract|need)\b"
    r"|\b(?:emit(?:s|ted|ting)?|print(?:s|ed|ing)?|output(?:s|ted|ting)?)\b"
    r"[^\n.!?]{0,80}$"
    r"|\b(?:validat(?:e|es|ed|ing)|pars(?:e|es|ed|ing)|tokeniz(?:e|es|ed|ing)|"
    r"serializ(?:e|es|ed|ing)|deserializ(?:e|es|ed|ing)|lint(?:s|ed|ing)|"
    r"assert(?:s|ed|ing)?)\s+$"
    r"|\b(?:reject(?:s|ing)?|warn(?:s|ed|ing)?|fail(?:s|ed|ing)?|"
    r"prevent(?:s|ed|ing)?|block(?:s|ed|ing)?|disallow(?:s|ed|ing)?|"
    r"detect(?:s|ed|ing)?|recommend(?:s|ed|ing)?)\b"
    r"[^\n.!?]{0,80}\s+$"
    r"|\b(?:the\s+)?(?:report|output|fixture|example|response|message|text|line)\s+"
    r"(?:should|must|will|can|contain(?:s|ed)?|include|say|show|display)\b"
    r"[^\n.!?]{0,80}$"
    r"|\b(?:generate|return|create|produce|write)\s+(?:an?\s+|the\s+)?"
    r"(?:ya?ml|json|manifest|config(?:uration)?|fixture|example|payload|snippet)\b"
    r"[^\n.!?]{0,120}$"
    r"|\b(?:the\s+)?generated\s+"
    r"(?:ya?ml|json|manifest|config(?:uration)?|fixture|example|payload|snippet)\b"
    r"[^\n.!?]{0,120}$"
    r"|\b(?:help\s+text|error\s+message)\b[^\n.!?]{0,160}$"
    r"|\b(?:create|write|add)\s+(?:the\s+)?(?:docs?|documentation)\s+"
    r"(?:with|containing|that|whose)\b[^\n.!?]{0,160}$"
    r"|\bexplain(?:s|ed|ing)?\s+why\b[^\n.!?]{0,80}$"
    r"|\bexplain(?:ed|ing)?\s+(?=(?:how\s+to\s+)?"
    r"(?:(?:use|set)\s+)?(?:task[_\s-]*type\b|inherit\b|derive\b|parent\b))"
    r"|\bshow(?:s|ed|n|ing)?\s+how\s+to\s+"
    r"(?:(?:use|set)\s+)?$"
    r"|\b(?:docs?\s+(?:say|says|said)|legacy\s+exports?)\b"
    r"|\b(?:for\s+reference|as\s+an?\s+reference|by\s+way\s+of\s+reference|"
    r"for\s+illustration|as\s+an?\s+illustration|"
    r"(?:the\s+)?docs?\s+(?:show|say|mention)|as\s+an?\s+example)\b"
    r"|(?:설정하지\s*마세요|(?:상속|계승)하면\s*안\s*(?:됩니다|돼요))",
    re.IGNORECASE,
)
_HISTORICAL_GOVERNOR_PATTERN = re.compile(
    r"(?:\b(?:the\s+)?(?:previous|prior|historical)\b"
    r"|\b(?:rejected|superseded|obsolete)\b[^\n.!?]{0,50}\b(?:proposal|contract|reference|request)\b)"
    r"[^\n.!?]{0,120}?\b(?:but|and|while|although|though|because|despite)\b"
    r"[^\n.!?]{0,80}$",
    re.IGNORECASE,
)
_HISTORICAL_PREFIX_PATTERN = re.compile(
    r"\b(?:in|from|under)\s+(?:the\s+)?(?:previous|prior|historical)\s+"
    r"(?:proposal|contract|reference|request)\b[^\n.!?]{0,120}$",
    re.IGNORECASE,
)
_NEGATIVE_GOVERNOR_PATTERN = re.compile(
    r"\b(?:no|instead\s+of|rather\s+than|it\s+is\s+false\s+that|false\s+that|no\s+longer)\b"
    r"[^\n.!?]{0,120}$",
    re.IGNORECASE,
)
_POST_MATCH_REJECTION_PATTERN = re.compile(
    r"^(?:(?:the|that)\s+)?(?:requirement|contract|proposal)?\s*"
    r"(?:(?:was|is|has\s+been)?\s*(?:rejected|superseded|obsolete|discarded)"
    r"|not\s+(?:used|required|needed|adopted|applied)"
    r"|no\s+longer\s+(?:used|required|needed|adopted|applied)"
    r"|(?:unnecessary|unneeded)"
    r"|(?:am|is|are|was|were)\s+not\s+(?:used|required|adopted|applied)"
    r"|(?:isn't|isn’t|aren't|aren’t|wasn't|wasn’t|weren't|weren’t)\s+(?:used|required|adopted|applied)"
    r"|(?:am|is|are|was|were)\s+(?:unnecessary|unneeded)"
    r"|(?:need|needs)\s+not\s+(?:be\s+)?(?:used|required|adopted|applied)"
    r"|(?:which\s+)?(?:was|is|has\s+been)\s+(?:rejected|declined|abandoned)"
    r"|(?:will|would)\s+not\s+(?:be\s+)?(?:used|required|adopted|applied)"
    r"|(?:am|is|are|was|were)\s+no\s+longer\s+(?:used|required|adopted|applied)"
    r"|not\s+anymore"
    r"|(?:we\s+)?decided\s+against\s+it"
    r"|scratch\s+that(?=\s*$)"
    r"|(?:should|must|may)\s+not\s+(?:be\s+)?(?:used|required|adopted|applied)"
    r"|(?:should|must|may)\s+(?:be\s+)?avoided"
    r"|(?:value|requirement|contract|proposal)\s+(?:should|must|may)\s+not\s+(?:be\s+)?(?:used|required|adopted|applied|avoided)"
    r"|(?:value|requirement|contract|proposal)\s+(?:is|was)\s+not\s+allowed"
    r"|(?:is|was)\s+an?\s+example"
    r"|(?:is|was)\s+only\s+an?\s+example"
    r"|(?:am|is|are|was|were)\s+(?:only\s+)?giving\s+an?\s+example"
    r"|(?:please\s+)?disregard\s+(?:that|it|the\s+(?:requirement|contract|proposal|value))"
    r"|as\s+an?\s+example"
    r"|(?:하지\s*마세요|하면\s*안\s*(?:됩니다|돼요)))\b",
    re.IGNORECASE,
)
_STANDALONE_RETRACTION_PATTERN = re.compile(
    r"(?:^|[;.!?\n])\s*(?:(?:actually|but|however)\s*,?\s*)?"
    r"(?:scratch\s+that(?=\s*(?:[;.!?\n]|$))|"
    r"we\s+decided\s+against\s+it(?=\s*(?:[;.!?\n]|$))|"
    r"never\s+mind(?=\s*(?:[;.!?\n]|$))|"
    r"forget\s+(?:that|it)(?=\s*(?:[;.!?\n]|$))|"
    r"i\s+take\s+(?:that|it)\s+back(?=\s*(?:[;.!?\n]|$))|"
    r"cancel\s+it(?=\s*(?:[;.!?\n]|$))|"
    r"(?:that|it|the\s+(?:requirement|contract|proposal|value))\s+"
    r"(?:was|is)\s+(?:only\s+)?an?\s+example|"
    r"(?:i|we)\s+(?:am|are|was|were)\s+(?:only\s+)?giving\s+an?\s+example|"
    r"(?:please\s+)?disregard\s+(?:that|it|the\s+(?:requirement|contract|proposal|value))|"
    r"(?:cancel|withdraw|retract)\s+(?:(?:that|this|the)\s+)?"
    r"(?:(?:task[_\s-]*type|parent(?:_seed_id|\s+seed)?)\s+)?"
    r"(?:requirement|contract|proposal|value)|"
    r"(?:that|the)\s+(?:requirement|contract|proposal|value)\s+"
    r"(?:was|is)\s+(?:rejected|superseded|obsolete|discarded)|"
    r"(?:the\s+)?(?:requirement|contract|proposal|value)\s+"
    r"(?:is|was)\s+not\s+allowed)",
    re.IGNORECASE,
)

_PARENT_CONTRACT_PATTERN = re.compile(
    r"\b(?:inherit(?:ing)?(?:\s+from)?|derive(?:d)?\s+from|"
    r"(?:set\s+)?parent(?:_seed_id|\s+seed)?\s*(?:is|=|:|to))\s+"
    rf"{_SEED_ID_PATTERN}(?!{_SEED_ID_CONTINUATION_PATTERN})"
    rf"|(?<!\S){_SEED_ID_PATTERN}"
    rf"(?!{_SEED_ID_CONTINUATION_PATTERN})"
    r"(?:을|를)?\s*(?:계승|상속)(?!하지)",
    re.IGNORECASE,
)
_CONTRACT_TAIL_AMBIGUITY_PATTERN = re.compile(
    r"^\s*(?:,\s*)?(?:(?:only\s+)?(?:if|when|whenever|unless|whether)\b"
    r"|depending\b|otherwise\b|provided(?:\s+that)?\b|as\s+long\s+as\b"
    r"|pending\b|after\b|once\b|upon\b|assuming\b|contingent\s+on\b|subject\s+to\b)"
    r"|^\s*,?\s*but\s+(?:maybe|possibly|perhaps|alternatively)\b"
    rf"|^\s*(?:,\s*)?or\s+(?:{_TASK_TYPE_PATTERN}|{_SEED_ID_PATTERN})"
    rf"(?!{_SEED_ID_CONTINUATION_PATTERN})",
    re.IGNORECASE,
)
_CANDIDATE_BOUNDARY_PATTERN = re.compile(
    r"[,!?;.\n]|\b(?:and|but|instead|without|although|though|because|while|despite)\b",
    re.IGNORECASE,
)
_AMBIGUOUS_CONTRACT_PATTERN = re.compile(
    r"\b(?:if|when|whenever|unless|whether|either|otherwise|depending|choose\s+between|may|might|could)\b"
    r"|\bor\b",
    re.IGNORECASE,
)
_COMMA_NON_BINDING_GOVERNOR_PATTERN = re.compile(
    r"(?:\b(?:for\s+(?:an?\s+)?example|for\s+reference|as\s+an?\s+reference|"
    r"by\s+way\s+of\s+reference|for\s+illustration|as\s+an?\s+illustration|"
    r"as\s+an?\s+example)"
    r"|\be\.g\.)\s*,\s*$"
    r"|\b(?:do\s+not|don't|doesn't|never|cannot|can't)\s*,"
    r"[^;.!?\n]{0,120},\s*(?:(?:use|set|select|choose)\s+)?$",
    re.IGNORECASE,
)
_ARTIFACT_PAYLOAD_GOVERNOR_PATTERN = re.compile(
    r"\b(?:generate|return|create|produce|write)\s+(?:an?\s+|the\s+)?"
    r"(?:ya?ml|json|manifest|config(?:uration)?|fixture|example|payload|snippet)\b"
    r"[^\n.!?]{0,240}$"
    r"|\b(?:the\s+)?generated\s+"
    r"(?:ya?ml|json|manifest|config(?:uration)?|fixture|example|payload|snippet)\b"
    r"[^\n.!?]{0,240}$"
    r"|\b(?:help\s+text|error\s+message)\b[^\n.!?]{0,240}$"
    r"|\b(?:create|write|add)\s+(?:the\s+)?(?:docs?|documentation)\s+"
    r"(?:with|containing|that|whose)\b[^\n.!?]{0,240}$"
    # Fail closed for control-looking text governed by an artifact-content
    # clause.  This is syntax based rather than an allowlist of README/TOML/
    # test/etc. nouns: callers can state a real Seed contract in a separate
    # imperative clause.
    r"|\b(?:build|implement|create|add)\b[^\n.!?]{0,240}"
    r"\b(?:whose|where|containing|that)\b[^\n.!?]{0,160}$"
    r"|\b(?:generate|return|produce|write)\b[^\n.!?]{0,240}"
    r"\b(?:whose|where|with|containing|that|saying|stating|showing|reading)\b"
    r"[^\n.!?]{0,160}$"
    r"|\b(?:the|a|an)\s+[^\n.!?]{1,120}\b(?:must|should|will)\s+"
    r"(?:set|contain|include|say|show|state|read|emit|print|output)\b"
    r"[^\n.!?]{0,160}$"
    r"|\b(?:add|update|fix|implement|support|test|validate|accept|rename|refactor|deprecate|remove|migrate)\b"
    r"[^\n.!?]{0,180}\b(?:task[_\s-]*type|parent_seed_id)\b[^\n.!?]{0,120}$"
    r"|\b(?:add|update|fix|implement|support|test|validate|accept)\b"
    r"[^\n.!?]{0,180}\b(?:when|whether|how)\b[^\n.!?]{0,120}"
    r"\b(?:inherit(?:ing)?|derive(?:d)?\s+from)\b[^\n.!?]{0,100}$"
    r"|\b(?:analyze|explain|investigate|describe|review|understand)\b"
    r"[^\n.!?]{0,240}\b(?:task[_\s-]*type|parent_seed_id|inherit(?:ing)?)\b"
    r"[^\n.!?]{0,120}$"
    # API/schema/parser prose describes the artifact being implemented.  A
    # field-shaped value in that clause is data-plane content, not authority
    # to reroute this Seed or rewrite its lineage.
    r"|\b(?:ensure\s+)?(?:the\s+)?(?:api|schema(?:\s+property)?|parser)\b"
    r"[^\n.!?]{0,240}$"
    r"|\b(?:persist|store|expose|record|log)\b[^\n.!?]{0,200}"
    r"\b(?:task[_\s-]*type|parent_seed_id)\b[^\n.!?]{0,120}$"
    r"|\b(?:the\s+)?(?:config(?:uration)?(?:\s+field)?|database(?:\s+column)?|"
    r"session\s+state|cli\s+output|log(?:ging)?\s+(?:field|output))\b"
    r"[^\n.!?]{0,200}\b(?:task[_\s-]*type|parent_seed_id)\b[^\n.!?]{0,120}$"
    # A field value governed as an implementation operand is data, independent
    # of domain nouns. Explicit routing/inheritance verbs remain authoritative.
    r"|\b(?!(?:use|set|select|choose|keep|inherit|derive|implement|deliver|produce)\b)"
    r"[A-Za-z][A-Za-z0-9_-]*\s+(?:the\s+)?"
    r"(?:task[_\s-]*type|parent_seed_id)\b[^\n.!?]{0,100}"
    r"\b(?:by|during|while|into|for)\b[^\n.!?]{0,160}$",
    re.IGNORECASE,
)
_EXPLICIT_TASK_TYPE_BINDING_PATTERN = re.compile(
    r"\b(?:implement|build|create|deliver|produce)\b\s+"
    r"(?:this|it|the\s+(?:seed|task|result|output|requested\s+"
    r"(?:seed|task|result|output|document|artifact|presentation|research|analysis)))\s+"
    r"(?:as|with)\s+"
    r"(?:task[_\s-]*type|task\s+type)\s*(?:is|=|:|to)?\s*"
    rf"{_TASK_TYPE_PATTERN}\b",
    re.IGNORECASE,
)


def _candidate_segment(text: str, start: int, end: int) -> tuple[int, int]:
    """Return the punctuation-or-conjunction-bounded candidate clause."""
    segment_start = 0
    for boundary in _CANDIDATE_BOUNDARY_PATTERN.finditer(text, 0, start):
        # A leading ``without``/causal clause is itself often the negation
        # governing the contract (``without using task_type``).  Keep it in
        # the candidate so the non-binding guard can reject it; these words
        # still delimit a positive contract when they occur after the match.
        if boundary.group().strip().casefold() in {"without", "despite"}:
            continue
        segment_start = boundary.end()
    next_boundary = _CANDIDATE_BOUNDARY_PATTERN.search(text, end)
    segment_end = next_boundary.start() if next_boundary is not None else len(text)
    return segment_start, segment_end


def _governor_scope_start(text: str, start: int) -> int:
    """Return the nearest hard clause boundary before a contract."""
    boundary = max(text.rfind(token, 0, start) for token in (";", ".", "!", "?", "\n"))
    return boundary + 1


def has_affirmative_contract_prefix(prefix: str, *, allow_task_linker: bool = False) -> bool:
    """Require field-shaped values to be current-Seed authority, not operands."""
    normalized = prefix.strip().casefold()
    if re.fullmatch(r"(?:(?:a|answer|correction)\s*:|actually\s*,?|the|please)?", normalized):
        return True
    return allow_task_linker and re.search(r"\b(?:use|with)\s*$", normalized) is not None


def explicit_task_type_from_goal(goal: str) -> str | None:
    """Return the task type only when the goal states a binding contract."""
    normalized = goal
    matches: list[tuple[int, int, str]] = []
    for pattern in _TASK_TYPE_CONTRACT_PATTERNS:
        for match in pattern.finditer(normalized):
            line_start = normalized.rfind("\n", 0, match.start()) + 1
            line_end = normalized.find("\n", match.end())
            if line_end < 0:
                line_end = len(normalized)
            line = normalized[line_start:line_end]
            segment_start, segment_end = _candidate_segment(normalized, match.start(), match.end())
            segment = normalized[segment_start:segment_end]
            governor_prefix = normalized[
                _governor_scope_start(normalized, match.start()) : match.start()
            ]
            segment_terminator = normalized[segment_end : segment_end + 1]
            if re.match(r"\s*(?:q|question|interviewer)\s*:", line, re.IGNORECASE):
                continue
            if "?" in segment or segment_terminator == "?":
                continue
            if re.match(r"\s*\?", normalized[match.end() :]):
                continue
            contract_prefix = normalized[segment_start : match.start()]
            candidate_through_match = normalized[segment_start : match.end()]
            artifact_governed = (
                _ARTIFACT_PAYLOAD_GOVERNOR_PATTERN.search(candidate_through_match) is not None
                or _ARTIFACT_PAYLOAD_GOVERNOR_PATTERN.search(segment) is not None
                or _ARTIFACT_PAYLOAD_GOVERNOR_PATTERN.search(governor_prefix) is not None
            )
            if (
                is_non_binding_contract_segment(contract_prefix)
                or is_non_binding_contract_segment(normalized[segment_start : match.end()])
                or not has_affirmative_contract_prefix(contract_prefix, allow_task_linker=True)
                and _EXPLICIT_TASK_TYPE_BINDING_PATTERN.search(candidate_through_match) is None
                or artifact_governed
                and _EXPLICIT_TASK_TYPE_BINDING_PATTERN.search(candidate_through_match) is None
                or is_quoted_contract(normalized, match.start(), match.end())
                or has_historical_governor(
                    normalized, match.start(), _governor_scope_start(normalized, match.start())
                )
                or has_negative_governor(
                    normalized, match.start(), _governor_scope_start(normalized, match.start())
                )
                or has_comma_non_binding_governor(normalized, match.start())
                or has_post_match_rejection(normalized, match.end())
                or _has_contract_local_ambiguity(normalized, match.end(), segment_end)
                or has_optional_contract_tail(normalized, match.end(), segment_end)
                or has_ambiguous_contract_governor(governor_prefix)
            ):
                continue
            matches.append((match.start(), match.end(), match.group("task_type").casefold()))
    if not matches:
        return None
    return _resolve_authoritative_matches(normalized, matches)


def is_non_binding_contract_segment(segment: str) -> bool:
    """Return whether a clause describes negated, historical, or quoted text."""
    return _NON_BINDING_CONTRACT_PATTERN.search(segment) is not None


def is_quoted_contract(text: str, start: int, end: int) -> bool:
    """Return whether the matched contract itself is enclosed in quotes."""
    before = text[:start]
    after = text[end:]
    for quote in ('"', "`"):
        if before.count(quote) % 2 == 1 and after.count(quote) > 0:
            return True
    left = before.rfind("'")
    right = after.find("'")
    if left >= 0 and right >= 0:
        left_boundary = left == 0 or not before[left - 1].isalnum()
        right_boundary = right + 1 == len(after) or not after[right + 1].isalnum()
        if left_boundary and right_boundary:
            return True
    for opening, closing in (("“", "”"), ("‘", "’")):
        if before.count(opening) > before.count(closing) and after.count(closing) > 0:
            return True
    return False


def is_ambiguous_contract_segment(segment: str) -> bool:
    """Reject conditional or multi-option language at an authority boundary."""
    return _AMBIGUOUS_CONTRACT_PATTERN.search(segment) is not None


def has_ambiguous_contract_governor(prefix: str) -> bool:
    """Reject ambiguity governing this clause, not unrelated earlier clauses."""
    but_resets = tuple(re.finditer(r"\bbut\b", prefix, re.IGNORECASE))
    if but_resets:
        scope = prefix[but_resets[-1].end() :]
    elif re.search(r"\b(?:if|when|whenever|unless|whether)\b", prefix, re.IGNORECASE):
        scope = prefix
    else:
        resets = tuple(re.finditer(r",|\b(?:and|while|although|though)\b", prefix, re.IGNORECASE))
        scope = prefix[resets[-1].end() :] if resets else prefix
    return is_ambiguous_contract_segment(scope)


def has_optional_contract_tail(text: str, end: int, segment_end: int) -> bool:
    """Reject optionality applied to the contract, not to a later output noun."""
    return re.match(r"\s+(?:is\s+)?optional\b", text[end:segment_end], re.IGNORECASE) is not None


def _is_explicit_correction(text: str, prior_end: int, next_start: int) -> bool:
    """Return whether the later surviving contract explicitly replaces the prior one."""
    gap = text[prior_end:next_start]
    hard_boundary = max(gap.rfind(token) for token in (";", ".", "!", "?", "\n"))
    correction_scope = gap[hard_boundary + 1 :]
    return (
        re.search(
            r"\b(?:actually|correction|corrected|instead|supersed(?:e|ed|ing))\b|정정|대신",
            correction_scope,
            re.IGNORECASE,
        )
        is not None
    )


def _retraction_cancels_candidate(text: str, candidate_end: int) -> bool:
    """Scope a later cancellation to its nearest task/lineage contract."""
    candidates = [
        (match.end(), "task_type")
        for pattern in _TASK_TYPE_CONTRACT_PATTERNS
        for match in pattern.finditer(text)
    ]
    candidates.extend((match.end(), "parent") for match in _PARENT_CONTRACT_PATTERN.finditer(text))
    for retraction in _STANDALONE_RETRACTION_PATTERN.finditer(text, candidate_end):
        wording = retraction.group(0).casefold()
        target = None
        if re.search(r"task[_\s-]*type", wording):
            target = "task_type"
        elif re.search(r"\bparent(?:_seed_id|\s+seed)?\b", wording):
            target = "parent"
        prior = [
            end
            for end, kind in candidates
            if end <= retraction.start() and (target is None or kind == target)
        ]
        if prior and max(prior) == candidate_end:
            return True
    return False


def _resolve_authoritative_matches(text: str, matches: list[tuple[int, int, str]]) -> str | None:
    """Resolve every ordered value transition, allowing duplicate confirmations."""
    ordered = sorted(
        match for match in matches if not _retraction_cancels_candidate(text, match[1])
    )
    if not ordered:
        return None
    current = ordered[0][2]
    prior_end = ordered[0][1]
    for start, end, value in ordered[1:]:
        if value != current and not _is_explicit_correction(text, prior_end, start):
            return None
        current = value
        prior_end = end
    return current


def has_historical_governor(text: str, start: int, scope_start: int | None = None) -> bool:
    """Return whether a preceding clause marks this contract as historical."""
    line_start = text.rfind("\n", 0, start) + 1
    prefix = text[max(line_start, scope_start or line_start) : start]
    return (
        _HISTORICAL_GOVERNOR_PATTERN.search(prefix) is not None
        or _HISTORICAL_PREFIX_PATTERN.search(prefix) is not None
    )


def has_negative_governor(text: str, start: int, scope_start: int | None = None) -> bool:
    """Return whether a preceding phrase explicitly negates this contract."""
    line_start = text.rfind("\n", 0, start) + 1
    return (
        _NEGATIVE_GOVERNOR_PATTERN.search(text[max(line_start, scope_start or line_start) : start])
        is not None
    )


def has_comma_non_binding_governor(text: str, start: int) -> bool:
    """Return whether a comma-separated marker still governs the contract.

    Commas usually separate candidate clauses, but natural reference markers
    (``For example,``) and interrupted negations (``Do not, ever, inherit``)
    remain semantically attached to the contract that follows them.
    """
    line_start = text.rfind("\n", 0, start) + 1
    return _COMMA_NON_BINDING_GOVERNOR_PATTERN.search(text[line_start:start]) is not None


def has_post_match_rejection(text: str, end: int) -> bool:
    """Return whether the current sentence immediately rejects the contract."""
    tail = text[end:]
    sentence_end = re.search(r"[.!?\n]", tail)
    suffix = tail[: sentence_end.start()] if sentence_end is not None else tail
    candidate = suffix.lstrip(" \t,;:")
    candidate = re.sub(
        r"^(?:but|and|while|although|though|because|despite)\s+",
        "",
        candidate,
        flags=re.IGNORECASE,
    )
    return _POST_MATCH_REJECTION_PATTERN.search(candidate) is not None


def _has_contract_local_ambiguity(text: str, end: int, segment_end: int) -> bool:
    """Reject ambiguity immediately attached to the matched contract only."""
    del segment_end
    hard_boundary = re.search(r"[;.!?\n]", text[end:])
    tail_end = end + hard_boundary.start() if hard_boundary is not None else len(text)
    return _CONTRACT_TAIL_AMBIGUITY_PATTERN.search(text[end:tail_end]) is not None


def normalize_task_type(value: object) -> str:
    """Return a supported task type or raise a precise extraction error."""
    task_type = str(value).strip().casefold()
    if task_type not in SUPPORTED_TASK_TYPES:
        valid = ", ".join(sorted(SUPPORTED_TASK_TYPES))
        raise ValueError(f"Invalid TASK_TYPE: {task_type!r}. Expected one of: {valid}")
    return task_type
