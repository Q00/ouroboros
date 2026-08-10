"""Ledger-derived task-class inference (L1-b of #1157 / #1171).

The Socratic interview already extracts structured `SeedDraftLedger`
entries (`actors`, `inputs`, `outputs`, `runtime_context`, …) and
*standardizes them toward canonical vocabulary* (e.g. "do you mean
stdout, stderr, or both?"). L1-b derives the task class from those
already-standardized entries by deterministic pattern matching — no
new LLM call, no eval set, no accuracy floor.

The matcher returns one of three outcomes:

- ``DomainInference.single(...)`` — exactly one class predicate fired.
- ``DomainInference.ambiguous(...)`` — multiple classes fired; the
  interview driver should ask a disambiguation question (L1-c).
- ``DomainInference.single(LIBRARY, reason="unmatched")`` — no
  predicate fired; falls to the safest default and emits a
  ``domain_unmatched`` telemetry signal so maintainers can grow the
  catalog.

Adding a new task class starts with a ``_matches_<name>`` function, a
``_PATTERN_REGISTRY`` entry, and a unit test — but the real obligation
is auditing the classes the new vocabulary overlaps (#1813 landed
``web_app`` against games, libraries, and CLI tooling): shared words
need an explicit ownership rule, goal-side denials must route through
the shared negation primitive, and outcomes must be checked against the
durable consumers, since ``active_task_class`` persistence and
default-AC injection accept only a single match while ambiguity and
unmatched fall back silently.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from functools import cache
import re

from ouroboros.auto.ledger import LedgerSection, LedgerStatus, SeedDraftLedger
from ouroboros.auto.task_classes import TaskClass
from ouroboros.auto.web_ownership import (
    _ADVERSATIVE_PIVOT_RE,
    _BARE_ADJACENT_QUALIFIER_RE,
    _BROWSER_COMPONENT_RE,
    _BROWSER_QUALIFIED_UI_HEAD_RE,
    _COMPONENT_ARTIFACT_FRAGMENT,
    _ENGINE_CONTAINMENT_RE,
    _EXPLICIT_WEB_VOCAB_RE,
    _GOAL_COMPONENT_IDENTITY_RE,
    _GOAL_COMPONENT_TARGET_RE,
    _INTERACTION_HEAD_TAIL_RE,
    _NOMINAL_GERUND_FRAGMENT,
    _NON_BROWSER_RUNTIME_RE,
    _POSTNOMINAL_BROWSER_QUALIFIER_RE,
    _POSTPOSITIVE_BROWSER_DENIAL_RE,
    _PRODUCED_SERVICE_RE,
    _PRODUCED_SERVICE_RESPONSE_RE,
    _RUNTIME_BROWSER_CONSUMER_RE,
    _SECTION_BROWSER_ENV_RE,
    _SUBJECT_COMPOUND_NOUN_FRAGMENT,
    _UI_PRODUCT_HEAD_FRAGMENT,
    _WEB_APP_ARTIFACT_PHRASE_RE,
    _WEB_APP_GOAL_SIGNAL_FRAGMENT,
    _WEB_APP_GOAL_SIGNAL_RE,
    _WEB_SERVICE_SIGNAL_FRAGMENT,
    _WEB_SIMILARITY_MODIFIER_RE,
    _WEBHOOK_RECEIVER_RE,
    _WEBHOOK_SIGNAL_FRAGMENT,
    _goal_first_np_has_ui_shape,
    _goal_first_np_head,
    _goal_first_np_is_ui_headed,
)

# Word-boundary token regex for the single-word "cli" goal signal. Avoids
# false-positive substring matches against words that happen to contain
# the letters "cli" (e.g. "client", "click", "clinic", "command-clinic")
# now that `goal_signal` is independently sufficient (see review on PR
# #1264 / #1170 R2 follow-up).
_CLI_GOAL_TOKEN_RE = re.compile(r"\bcli\b")

# Multi-word "command line" / "command-line" phrase regex. Not vulnerable
# to the substring-false-friend class, but folded into the same negation
# pipeline below so "not a command-line tool" is rejected consistently
# with "not a CLI".
_CLI_GOAL_PHRASE_RE = re.compile(r"\bcommand[\s\-]line\b")

# Explicit-negation pattern matching common natural-language denials of
# the CLI signal. The earlier whitelist-of-connectors strategy
# (rounds 2-6) proved too narrow — every review round surfaced another
# descriptive participle that was missing from the list ("intended",
# "meant", "designed", "exposed", "for", …). Switching to a generic
# distance-bounded path with an *affirmative-qualifier blocklist* is
# both more robust and easier to reason about:
#
#   <negation cue>   <path of up to 7 tokens>   <cli|command-line>
#
# The strip is *suppressed* if the path contains an affirmative-flip
# qualifier — "just", "only", "also", "rather", "but" — because those
# tokens turn the phrase into an affirmative expansion ("not just a
# CLI", "not only a CLI", "not a webhook but rather a CLI") that
# genuinely asserts CLI. Those cases must keep their CLI signal.
#
# Covers the shapes flagged across rounds 2-7:
#   - Direct:           "not a CLI", "no CLI", "never a CLI",
#                       "isn't a CLI".
#   - Modal/copular:    "should not be a CLI", "cannot be a CLI",
#                       "shouldn't be a CLI", "doesn't have to be a CLI".
#   - Exclusion:        "without a CLI", "excluding a CLI", "sans CLI",
#                       "instead of a CLI", "rather than a CLI".
#   - Participle:       "not intended to be a CLI", "not meant to be
#                       a CLI", "not designed to be a CLI",
#                       "not exposed as a CLI", "not for CLI use".
#
# Each variant also works for the multi-word "command line" /
# "command-line" phrase. See PR #1264 review rounds 2-7.
_NEGATION_CUE_FRAGMENT = (
    r"(?:not|no|never|"
    r"without|excluding|excludes?|omit(?:s|ted)?|sans|"
    # Exclusion predicates deny like negators (#1813 R112): "avoid a
    # browser UI" rejects the UI it names.
    r"avoid(?:s|ed|ing)?|skips?|skipping|skipped|forgo(?:es|ing|ne)?|"
    r"eschews?|eschewing|refrains?\s+from|refraining\s+from|"
    r"isn[’']?t|aren[’']?t|wasn[’']?t|weren[’']?t|"
    r"won[’']?t|wouldn[’']?t|shouldn[’']?t|"
    r"can[’']?t|cannot|"
    r"doesn[’']?t|don[’']?t|didn[’']?t|"
    r"instead\s+of|rather\s+than|as\s+opposed\s+to)"
)
_NEGATED_CLI_GOAL_RE = re.compile(
    rf"\b(?P<cue>{_NEGATION_CUE_FRAGMENT})"
    r"(?P<path>(?:\s+\S+){0,7}?)"  # Up to 7 intervening tokens (non-greedy).
    r"\s+(?:cli|command[\s\-]line)\b"
)

# Words that, when they appear in the intervening path between a
# negation cue and the CLI signal, flip the phrase into an affirmative
# expansion. "not just a CLI" / "not only a CLI" mean "a CLI AND
# something else"; "but rather a CLI" / "not X but a CLI" mean "the
# tool IS a CLI". When any of these appears in a candidate strip span,
# the strip is suppressed so the positive CLI signal survives.
_AFFIRMATIVE_FLIP_RE = re.compile(r"\b(?:just|only|also|rather|but)\b")

# Prefix-style negation ("non-CLI" / "non CLI" / "non-command-line").
# `\b` boundaries prevent false matches on unrelated words containing
# the letters (e.g. "non-client" or "non-clinic" — the `cli\b` boundary
# fails because the following letters extend the word). Tracked
# separately from `_NEGATED_CLI_GOAL_RE` because the cue ("non") is
# directly fused to the CLI token rather than separated by connectors.
# See PR #1264 review round 6.
_NEGATED_CLI_PREFIX_RE = re.compile(r"\bnon[\s\-]?(?:cli|command[\s\-]line)\b")


def _goal_has_unnegated_cli_signal(goal_text: str) -> bool:
    """Return True iff *goal_text* contains a CLI signal that is not
    inside a recognized negation clause.

    Strategy: detect any positive CLI signal (token or multi-word
    phrase), then strip recognized negation wrappers from the text and
    re-check. If a positive signal survives the strip, the goal
    genuinely asserts CLI.
    """
    # "neither X nor Y" denies both alternatives here too (#1813
    # R102/R115) — the CLI path shares the rule with the negation-strip
    # primitive.
    goal_text = _NEITHER_NOR_SPAN_RE.sub(" ", goal_text)
    has_signal = bool(_CLI_GOAL_TOKEN_RE.search(goal_text)) or bool(
        _CLI_GOAL_PHRASE_RE.search(goal_text)
    )
    if not has_signal:
        return False

    def _strip_if_not_affirmative(match: re.Match[str]) -> str:
        """Drop the matched negation span unless its **path** (the text
        between the negation cue and the CLI token) contains an
        affirmative-flip qualifier (just/only/also/rather/but), in
        which case the phrase is actually an affirmative expansion and
        the CLI signal must survive.

        The cue itself ("rather than", "instead of", …) is excluded
        from the affirmative check on purpose — otherwise the
        "rather" inside the cue "rather than" would mis-block the
        strip for legitimate negation phrases like "rather than a
        CLI".
        """
        path = match.group("path") or ""
        if _AFFIRMATIVE_FLIP_RE.search(path):
            return match.group(0)
        return " "

    stripped = _NEGATED_CLI_GOAL_RE.sub(_strip_if_not_affirmative, goal_text)
    stripped = _NEGATED_CLI_PREFIX_RE.sub(" ", stripped)
    return bool(_CLI_GOAL_TOKEN_RE.search(stripped)) or bool(_CLI_GOAL_PHRASE_RE.search(stripped))


@cache
def _negation_res_for(signal_fragment: str) -> tuple[re.Pattern[str], re.Pattern[str]]:
    """Compile (negated-span, non-prefix) patterns for one signal family.

    The negated span is <cue> <path> <denied signal> with coordinated
    alternatives ("not an X or Y") and trailing descriptive words consumed
    up to a guarded connector/punctuation boundary — the discipline built
    up for the web-app family across #1813 R1-R8, shared so per-class
    denial semantics cannot drift (#1813 R12).
    """
    denied = rf"{signal_fragment}(?:[\s\-](?!(?:or|and|nor|but|rather|instead)\b)\w+)*"
    negated = re.compile(
        rf"\b(?P<cue>{_NEGATION_CUE_FRAGMENT})"
        # Path tokens cannot carry punctuation (#1813 R26/W1): a denial in
        # one clause must not consume the next clause's artifact — commas
        # included, since a feature denial ("with no signup, plus a
        # browser UI") must not reach the next conjunct.
        r"(?P<path>(?:\s+[^\s.;!?,]+){0,7}?)"
        rf"\s+{denied}"
        # Coordinated alternatives may lead with a couple of modifier
        # words ("not X, interactive web app, or responsive frontend").
        rf"(?:(?:\s*,\s*(?:or\s+|and\s+|nor\s+)?|\s+(?:or|and|nor)\s+|\s*/\s*)"
        rf"(?:an?\s+|the\s+)?(?:[\w\-]+\s+){{0,2}}?{denied})*\b"
    )
    prefix = re.compile(rf"\bnon[\s\-]?{signal_fragment}\b")
    return negated, prefix


_QUALITY_NOUN_RE = re.compile(
    r"\b(?:errors?|issues?|bugs?|warnings?|problems?|defects?|failures?|crashes?)\b"
)


# The span stops at adversative but/yet — the pivot survives (#1813 R121).
_NEITHER_NOR_SPAN_RE = re.compile(r"\bneither\b[^,.;]*?\bnor\b(?:(?!\s(?:but|yet)\b)[^,.;])*")


def _strip_negated_signals(text: str, signal_fragment: str) -> str:
    """Remove recognized denials of *signal_fragment* from *text*, keeping
    affirmative-flip expansions ("not just a browser page") intact."""
    # "neither X nor Y" denies BOTH alternatives for every class (#1813
    # R102) — the whole span leaves before any signal is read, so a
    # denied alternative can never become a match or a co-product.
    text = _NEITHER_NOR_SPAN_RE.sub(" ", text)
    negated, prefix = _negation_res_for(signal_fragment)

    def _keep_if_affirmative(match: re.Match[str]) -> str:
        path = match.group("path") or ""
        # A quality noun in the path means the negation targets the
        # defect, not the artifact: "no errors in the signup form" keeps
        # its form (#1813 R22), like the affirmative-flip expansions.
        if _AFFIRMATIVE_FLIP_RE.search(path) or _QUALITY_NOUN_RE.search(path):
            return match.group(0)
        return " "

    return prefix.sub(" ", negated.sub(_keep_if_affirmative, text))


_ARTIFACT_HEAD_NOUNS = frozenset(
    {
        "app",
        "apps",
        "application",
        "applications",
        "webapp",
        "webapps",
        "ui",
        "uis",
        "interface",
        "interfaces",
        "page",
        "pages",
        "frontend",
        "frontends",
        "site",
        "sites",
        "website",
        "websites",
        "dashboard",
        "dashboards",
        "console",
        "consoles",
        "portal",
        "portals",
    }
)
# Reason/purpose tails do not change what is denied (#1813 R36): "not a
# web app because of accessibility constraints" denies the web app.
_DENIED_PP_TAIL_RE = re.compile(r"\b(?:for|about|because|since|due\s+to|owing\s+to)\b.*$")
_DENIED_PIECE_SPLIT_RE = re.compile(r"\s*(?:,|/|\bor\b|\band\b|\bnor\b)\s*")


# Subject-matter clauses name the topic, never the artifact (#1813 R66).
_ABOUT_CLAUSE_RE = re.compile(r"\b(?:about|regarding|concerning)\s+[^,.;]*")
# Broad clause strip for the web-app guard: any for/with/about/that/which
# clause is subject matter FROM THE WEB APP'S PERSPECTIVE — it cannot
# surrender web ownership (#1813 R16/R18).
_SUBJECT_CLAUSE_RE = re.compile(r"\b(?:for|with|about)\s+[^,.;]*|\b(?:that|which)\s+[^,.;]*")
# Narrow strip for the library matcher's own evidence (#1813 R19): only
# clauses whose verb marks displayed/managed CONTENT are subject matter
# ("that displays SDK documentation"); clauses that define the artifact
# ("which exposes an SDK", "with an importable public API", "that is
# importable") keep their library shape.
_LIBRARY_SUBJECT_CLAUSE_RE = re.compile(
    r"\b(?:for|about)\s+(?![^,.;]*\b(?:sdks?|librar|importable|import|packages?)\w*)[^,.;]*"
    r"|\b(?:that|which)\s+"
    r"(?:displays?|shows?|renders?|tracks?|manages?|lists?|visualizes?|monitors?|documents?)"
    r"\b[^,.;]*"
)


# Prefix denials parsed through the artifact head noun (#1813 R19):
# "non-browser user interface" denies an interface, not just "browser".
_WEB_APP_PREFIX_DENIAL_RE = re.compile(
    rf"\bnon[\s\-]?{_WEB_APP_GOAL_SIGNAL_FRAGMENT}"
    r"(?:[\s\-](?!(?:or|and|nor|but|rather|instead)\b)\w+){0,3}"
)


_WEB_SUBTYPE_RE = re.compile(r"single[\s\-]page")


_TRAILING_ADVERBS = frozenset(
    {"anymore", "anyway", "either", "though", "whatsoever", "now", "yet", "all", "at"}
)
# Content clauses end at punctuation OR adversative coordination (#1813
# R34): "for frontend developers but not a web app" returns to the main
# artifact claim at "but".
_CONTENT_CLAUSE_RE = re.compile(
    r"\b(?:that|which|for|about)\s+[^,.;]*?"
    r"(?=\s+but\b|\s+yet\b|\s+although\b|\s+though\b|\s+whereas\b|"
    r"\s+as\s+opposed\s+to\b|[,.;]|$)"
)


def _goal_denies_web_app_artifact(goal_text: str) -> bool:
    """True when a denial rejects the app/UI artifact type itself.

    Dominance keys on the head noun of each denied alternative (#1813
    R16): "not a web app" and "non-web-app" deny the artifact, while
    "not a frontend SDK" denies an SDK and "not a browser extension for
    login pages" denies an extension — the modifier "frontend" and the
    PP object "pages" are not what is being denied.
    """
    # Denials inside content clauses ("catalogs sites which are not a web
    # application") describe that clause's subject, not the produced
    # artifact (#1813 R31): a span STARTING inside a that/which/for/about
    # region is content-scoped. Position-based scoping — rather than
    # pre-stripping the clause — keeps coordinated denials whose
    # alternatives extend past a comma intact.
    content_regions = [match.span() for match in _CONTENT_CLAUSE_RE.finditer(goal_text)]
    negated, prefix = _negation_res_for(_WEB_APP_GOAL_SIGNAL_FRAGMENT)
    spans = [
        match.group(0)
        for match in negated.finditer(goal_text)
        # Same keep-rules as the strip (#1813 R23): affirmative flips and
        # quality statements ("no errors in the web UI") are not denials.
        if not (
            _AFFIRMATIVE_FLIP_RE.search(match.group("path") or "")
            or _QUALITY_NOUN_RE.search(match.group("path") or "")
            or any(start <= match.start() < end for start, end in content_regions)
        )
    ]
    # A prefix denial is analyzed both bare ("non-web-app" denies an app)
    # and extended through trailing words ("non-browser user interface"
    # denies an interface) — either head noun dominates (#1813 R19).
    spans.extend(
        match.group(0)
        for match in prefix.finditer(goal_text)
        if not any(start <= match.start() < end for start, end in content_regions)
    )
    spans.extend(
        match.group(0)
        for match in _WEB_APP_PREFIX_DENIAL_RE.finditer(goal_text)
        if not any(start <= match.start() < end for start, end in content_regions)
    )
    for span in spans:
        core = _DENIED_PP_TAIL_RE.sub(" ", span)
        for piece in _DENIED_PIECE_SPLIT_RE.split(core):
            # Denying a narrower subtype ("not a single-page application")
            # rejects that subtype, not the web class (#1813 R24) — the
            # strip removes the span and survival semantics decide.
            if _WEB_SUBTYPE_RE.search(piece):
                continue
            words = re.findall(r"[a-z]+", piece)
            # Trailing adverbs ("not a web app anymore") do not change
            # what is denied (#1813 W1).
            while words and words[-1] in _TRAILING_ADVERBS:
                words.pop()
            if words and words[-1] in _ARTIFACT_HEAD_NOUNS:
                return True
    return False


def _goal_has_unnegated_web_app_signal(goal_text: str) -> bool:
    """Web-app twin of :func:`_goal_has_unnegated_cli_signal`, built on the
    shared negation primitive.

    An explicit artifact-type denial dominates (#1813 R14/R15): once the
    goal denies producing a web app or UI, remaining browser-domain
    mentions describe the tool's domain, not its artifact type. Denials of
    other browser-family artifacts ("not a browser extension") leave an
    independent affirmative web-app statement intact, and affirmative-flip
    spans ("not just a browser page") are preserved by the strip.
    """
    # Postpositive denials apply to the goal surface too (#1813 R40):
    # "web apps are unsupported" negates from behind on any surface.
    goal_text = _POSTPOSITIVE_BROWSER_DENIAL_RE.sub(" ", goal_text)
    if not _WEB_APP_GOAL_SIGNAL_RE.search(goal_text):
        return False
    if _goal_denies_web_app_artifact(goal_text):
        return False
    stripped = _strip_negated_signals(goal_text, _WEB_APP_GOAL_SIGNAL_FRAGMENT)
    return bool(_WEB_APP_GOAL_SIGNAL_RE.search(stripped))


__all__ = [
    "DomainInference",
    "derive_domain_from_ledger",
    "register_pattern",
]


_PatternFn = Callable[[SeedDraftLedger], bool]


@dataclass(frozen=True, slots=True)
class DomainInference:
    """Outcome of pattern-matching a ledger against the L1-a catalog.

    ``classes`` carries every class whose predicate fired:

    - len(classes) == 0 → unmatched; ``fallback`` carries the safe default
      and ``reason == "unmatched"``.
    - len(classes) == 1 → single, deterministic match; ``fallback`` is
      ``None``.
    - len(classes) >= 2 → ambiguous; ``fallback`` is ``None`` and the
      interview driver should disambiguate before proceeding.
    """

    classes: frozenset[TaskClass]
    reason: str
    fallback: TaskClass | None = None
    matched_signals: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_single(self) -> bool:
        return len(self.classes) == 1

    @property
    def is_ambiguous(self) -> bool:
        return len(self.classes) >= 2

    @property
    def is_unmatched(self) -> bool:
        return len(self.classes) == 0

    @property
    def single(self) -> TaskClass | None:
        """Return the single matched class, or ``fallback`` for unmatched.

        Ambiguous outcomes return ``None`` — callers should branch on
        :attr:`is_ambiguous` before reading this.
        """
        if self.is_single:
            return next(iter(self.classes))
        if self.is_unmatched:
            return self.fallback
        return None


# ---------------------------------------------------------------------------
# Pattern functions — one per class. Each consumes the ledger's already-
# standardized entries (lowercase substring matching after normalization)
# and returns True iff *its* class is plausible.
#
# Patterns are intentionally conservative: a pattern can fire even when
# another also fires (that produces an ambiguous DomainInference, which
# the interview driver disambiguates). A class never fires when the
# corresponding interview answer is absent or empty.
# ---------------------------------------------------------------------------


def _section_text(ledger: SeedDraftLedger, section: str) -> str:
    """Return the concatenated active-entry text for *section*, lowercased.

    Inactive statuses (WEAK / CONFLICTING / BLOCKED) are excluded — the
    interview standardizer's confirmed/defaulted/inferred entries are what
    represents the user's *current best understanding*.
    """
    sec: LedgerSection | None = ledger.sections.get(section)
    if sec is None:
        return ""
    inactive = {LedgerStatus.WEAK, LedgerStatus.CONFLICTING, LedgerStatus.BLOCKED}
    parts: list[str] = []
    for entry in sec.entries:
        if entry.status in inactive:
            continue
        if not entry.value:
            continue
        parts.append(entry.value)
    return "\n".join(parts).lower()


def _any_of(text: str, keywords: Iterable[str]) -> bool:
    return any(keyword in text for keyword in keywords)


# Replacement goals carry the replaced artifact as background (#1813
# R114): "Existing system is a CLI. Replace it with a web app" produces
# the web app. When a replacement directive is present, current-state
# segments describe what leaves, and the directive's own object is the
# replaced artifact up to the with/by phrase that names the product.
_REPLACEMENT_DIRECTIVE_RE = re.compile(
    r"\b(?:replac\w+|migrat\w+|supersed\w+|retir\w+|sunsett?\w*|"
    r"switch\w*\s+(?:from|to|away)|transition\w*|"
    r"mov\w+\s+(?:away\s+)?(?:from|to))\b"
)
_CURRENT_STATE_MARKER_RE = re.compile(
    r"\b(?:existing|currently|current|legacy|today|at\s+present|"
    r"as[\s\-]is|previous(?:ly)?)\b"
)
# Migration "from X to Y" keeps its own shape — the destination rule
# already reads Y as the produced artifact and needs the source phrase
# for its context.
_REPLACED_OBJECT_RE = re.compile(
    r"\b(?:replaces?|replacing|supersedes?|superseding|retires?|retiring|"
    r"sunsets?|sunsetting)\s+[^,.;]*?(?=\bwith\b|\bby\b|[,.;]|$)"
)


def _strip_replaced_background(text: str) -> str:
    if not _REPLACEMENT_DIRECTIVE_RE.search(text):
        return text
    kept = [
        segment
        for segment in re.split(r"(?<=[.;!?])", text)
        if not _CURRENT_STATE_MARKER_RE.search(segment) or _REPLACEMENT_DIRECTIVE_RE.search(segment)
    ]
    return _REPLACED_OBJECT_RE.sub(" ", "".join(kept))


def _goal_text(ledger: SeedDraftLedger) -> str:
    return _strip_replaced_background(_section_text(ledger, "goal"))


# Displayed content is the UI's subject, not the produced artifact
# (#1813 R107): "exit code and stdout shown in a status panel" and
# "REST API responses displayed to users" describe what the app renders
# from a consumed tool or service — the clause carries no ownership for
# the tool's own class.
_DISPLAYED_CONTENT_CLAUSE_RE = re.compile(
    r"[^,.;]*\b(?:shown|displayed|rendered|presented|surfaced|visualized|"
    r"visualised|streamed|mirrored|echoed)\s+(?:in|on|within|inside|to|for)\b[^,.;]*"
)


def _matches_cli(ledger: SeedDraftLedger) -> bool:
    outputs = _section_text(ledger, "outputs")
    runtime = _section_text(ledger, "runtime_context")
    if not (outputs or runtime):
        # Gate: require *some* ledger-side evidence beyond the goal text
        # so single-word "cli" mentions in goal cannot classify alone.
        return False
    output_signal = _any_of(
        _DISPLAYED_CONTENT_CLAUSE_RE.sub(" ", outputs),
        ("stdout", "exit code", "printed", "console output", "command output"),
    )
    runtime_signal = _any_of(runtime, ("shell", "terminal", "subprocess", "command line"))
    # A launcher shell/terminal is not CLI runtime identity (#1813 R121):
    # with an affirmed browser product it needs output corroboration.
    if runtime_signal and not output_signal:
        runtime_signal = not (
            _SECTION_BROWSER_ENV_RE.search(runtime)
            or _goal_artifact_head_is_web_app(_goal_text(ledger))
        )
    # Goal-side CLI signal: token-bounded "cli" OR explicit "command line"
    # / "command-line" multi-word phrase, with explicit-negation
    # stripping. Substring-only matching against "cli" would false-
    # positive on "client", "click", "clinic", etc. (first-round PR
    # #1264 blocker), and naked token matching still classifies "not a
    # CLI" / "no CLI" goals as CLI (second-round PR #1264 blocker), so
    # both classes route through `_goal_has_unnegated_cli_signal`.
    # Consumed tooling ("using a command-line image converter") is a
    # dependency, not the produced artifact (#1813 R28). Audience and
    # displayed-subject mentions own nothing either (#1813 R109): "for
    # CLI users" names who the product serves, "showing CLI command
    # examples" names what it displays.
    goal_text = _strip_audience_and_displayed_subjects(
        _strip_consumed_dependencies(_goal_text(ledger))
    )
    # Attributive CLI mentions follow the final-head rule shared with the
    # library and web-app vocabulary (#1813 R61): in "a CLI documentation
    # website" the produced artifact is the website, so the goal-side CLI
    # token carries no ownership when the first NP heads a UI product.
    goal_signal = _goal_has_unnegated_cli_signal(goal_text) and not _goal_first_np_is_ui_headed(
        _goal_text(ledger)
    )
    # Each signal is independently sufficient past the evidence gate;
    # nesting goal_signal under outputs made it dead code and blocked
    # ledger_only closures (#1170 R2, #1157 closure policy).
    return runtime_signal or goal_signal or output_signal


def _matches_webhook(ledger: SeedDraftLedger) -> bool:
    inputs = _section_text(ledger, "inputs")
    outputs = _section_text(ledger, "outputs")
    goal = _goal_text(ledger)
    if not (inputs or outputs or goal):
        return False
    # Denials, audiences, and dependencies own nothing (#1813 R121).
    webhook_goal = _strip_audience_and_displayed_subjects(
        _strip_consumed_dependencies(_strip_negated_signals(goal, _WEBHOOK_SIGNAL_FRAGMENT))
    )
    has_webhook_in = bool(_WEBHOOK_RECEIVER_RE.search(inputs + " " + webhook_goal))
    has_side_effect = _any_of(
        outputs,
        ("side effect", "db row", "database row", "log entry", "stored", "external call"),
    )
    return has_webhook_in and has_side_effect


def _matches_web_service(ledger: SeedDraftLedger) -> bool:
    outputs = _section_text(ledger, "outputs")
    goal = _goal_text(ledger)
    if not (outputs or goal):
        return False
    # Denied service vocabulary is not evidence (#1813 R32): the goal
    # routes through the shared negation strip before keyword matching.
    # Audience and displayed-subject mentions own nothing (#1813 R109):
    # "for REST API operators" and "showing REST API status" describe
    # the UI's audience and content.
    service_goal = _strip_audience_and_displayed_subjects(
        _strip_negated_signals(goal, _WEB_SERVICE_SIGNAL_FRAGMENT)
    )
    # A production verb governing the API ("serving predictions via a
    # REST API") keeps it produced even through via/through (#1813 W1).
    # Goal-side service evidence follows the final-head rule (#1813 R61):
    # in "a REST API documentation website" the API is the website's
    # subject, so a UI-headed goal contributes no service ownership —
    # outputs evidence (produced responses, endpoint lists) still owns.
    ui_headed_goal = _goal_first_np_is_ui_headed(goal)
    if not ui_headed_goal and _PRODUCED_SERVICE_RE.search(service_goal):
        return True
    # The sections are joined with a sentence boundary so the destination
    # rule sees the goal's own imperative segment (#1813 R53) — keyword
    # matching never legitimately spans the outputs/goal seam.
    # Displayed responses are the client's rendered content, not a
    # produced service (#1813 R107) — the display clause leaves before
    # keyword matching.
    visible = _strip_consumed_dependencies(
        _DISPLAYED_CONTENT_CLAUSE_RE.sub(" ", outputs)
        + ". "
        + ("" if ui_headed_goal else service_goal)
    )
    # Response nouns describe service ownership only when a server-like
    # artifact governs a production verb (#1813 R37).  Browser clients also
    # display HTTP responses / JSON bodies, so those detached payload nouns
    # cannot establish a service by themselves.
    if _PRODUCED_SERVICE_RESPONSE_RE.search(visible):
        return True
    api_signal = _any_of(
        visible,
        (
            "rest endpoint",
            "rest api",
            "multiple endpoints",
            "web service",
            "web server",
            "http server",
        ),
    )
    return api_signal


def _matches_data_pipeline(ledger: SeedDraftLedger) -> bool:
    inputs = _section_text(ledger, "inputs")
    outputs = _section_text(ledger, "outputs")
    if not (inputs and outputs):
        return False
    input_signal = _any_of(
        inputs,
        ("dataset", "csv", "parquet", "log file", "log files", "input file", "batch"),
    )
    output_signal = _any_of(
        outputs,
        ("aggregated", "transformed", "parquet", "summarized", "rolled up", "output dataset"),
    )
    return input_signal and output_signal


# "render" and "screen" are vocabulary shared between games and browser
# UIs ("Rendered sprites" vs "a login page is rendered on initial load";
# "game-over screen" vs "password reset screen"). Per-phrase lookbehinds
# cannot track grammatical variants (#1813 R5-R9), so ownership is decided
# by ledger context instead: in a ledger that carries browser context the
# shared words describe the UI and the game classifier cedes them, while
# the unshared game vocabulary (frame/canvas/scene/...) always counts.
# Game-domain vocabulary marks game ownership of the shared render/screen
# words even under browser deployment ("Build a browser game"), and
# symmetrically makes the web_app matcher cede — browser hosting does not
# turn game rendering into UI evidence (#1813 R10). The evidence must be
# affirmative and sense-specific (#1813 R11): "player" counts only in its
# game sense ("player movement"), never as a media player, and a negated
# goal mention ("not a game") is stripped before the check.
_GAME_DOMAIN_RE = re.compile(
    r"\b(?:games?|sprites?|collisions?|arcade|platformers?|shooters?|"
    r"players?\s+(?:movement|position|input|controls?|characters?))\b"
)
# Every goal-side game signal — the domain vocabulary above and the
# fast-path keywords alike — shares the negation treatment (#1813 R12):
# "not a platformer" and "without a canvas or game loop" are denials, not
# evidence. Outputs stay direct, as standardized evidence.
_GAME_GOAL_SIGNAL_FRAGMENT = (
    r"(?:game\s+loop|2d\s+games?|games?|sprites?|collisions?|arcade|"
    r"platformers?|shooters?|players?|canvas(?:es)?|frames?|scenes?|playable)"
)


# Core terms always own games. Canvas/scene/frame share rendering vocabulary
# with browser UIs and therefore need domain evidence (#1813 R18).
_GAME_CORE_RE = re.compile(r"\b(?:game\s+loops?|playable|2d\s+games?)\b")
_GAME_SHARED_SHAPE_RE = re.compile(
    r"\b(?:render(?:s|ing|ed)?|screens?|canvas(?:es)?|scenes?|frames?)\b"
)


def _game_visible_text(ledger: SeedDraftLedger) -> str:
    """Outputs plus the goal with negated game signals stripped."""
    goal = _strip_negated_signals(_goal_text(ledger), _GAME_GOAL_SIGNAL_FRAGMENT)
    return _section_text(ledger, "outputs") + " " + goal


def _matches_game_2d(ledger: SeedDraftLedger) -> bool:
    outputs = _section_text(ledger, "outputs")
    goal = _goal_text(ledger)
    if not (outputs or goal):
        return False
    visible = _game_visible_text(ledger)
    # Token-bounded (#1813 R13): substrings made "iframe" satisfy "frame".
    if _GAME_CORE_RE.search(visible):
        return True
    head_goal = _strip_negated_signals(goal, _GAME_GOAL_SIGNAL_FRAGMENT)
    # An adversative pivot re-heads the goal (#1813 R122): what follows
    # the last but/yet is the produced artifact.
    pivots = list(_ADVERSATIVE_PIVOT_RE.finditer(head_goal))
    if pivots:
        head_goal = head_goal[pivots[-1].end() :]
    goal_head = _goal_first_np_head(head_goal)
    if re.fullmatch(r"games?|platformers?|shooters?", goal_head or ""):
        return bool(_GAME_DOMAIN_RE.search(visible))
    if not _GAME_SHARED_SHAPE_RE.search(visible):
        return False
    # Shared render/screen/frame vocabulary requires POSITIVE game
    # ownership (#1813 R108): the absence of browser context is not a
    # game signal — a PDF generator rendering report pages to disk is
    # not a game because it isn't a web app.
    return bool(_GAME_DOMAIN_RE.search(visible))


def _matches_refactor_in_place(ledger: SeedDraftLedger) -> bool:
    goal = _goal_text(ledger)
    constraints = _section_text(ledger, "constraints")
    if not goal:
        return False
    refactor_intent = _any_of(
        goal,
        ("refactor", "rewrite", "restructure", "extract module", "split module"),
    )
    preserve_behaviour = _any_of(
        constraints + " " + goal,
        ("preserve behavior", "preserve behaviour", "same tests", "behaviour preserved"),
    )
    # Intent alone is enough; the constraint just strengthens confidence.
    return refactor_intent or (
        # Some users phrase as a constraint without saying "refactor" in goal.
        _any_of(goal, ("clean up", "tidy", "reorganize", "reorganise")) and preserve_behaviour
    )


# UI-composition evidence must denote a rendered/interactable UI product
# (#1813 R1/R3/R4): raw substrings fabricated the signal ("form" ⊂
# "performance"); token-bounded bare nouns misread library outputs
# ("validation helpers", "documentation pages"); and unconditional
# "interactive"/"client-side validation" misread docs and API-domain
# outputs ("interactive API documentation", "client-side validation
# helpers"). A term therefore counts only anchored to a product: a named
# widget ("signup form", "filters panel", "responsive dashboard"), an
# explicit UI verb phrase ("pages rendered", "form submit"),
# "interactive" bound to a rendered artifact, or "client-side validation"
# bound to user-facing feedback. Product-anchored "screen"s belong here
# (#1813 R8); the game predicate cedes them via _GAME_SCREEN_RE.
# UI-composition evidence needs a semantic product anchor (#1813 R6): any
# single word in front of a widget noun over-accepted API/documentation
# artifacts ("API reference pages", "example pages"), so the widget branch
# is allowlist-anchored to product-flow vocabulary on either side
# (anchor-first "login page" or noun-first "forms for login"), and the
# artifact tail also rejects compound API descriptions ("signup form
# validation helpers").
_UI_WIDGET_NOUN = r"(?:forms?|panels?|buttons?|dashboards?|pages?|screens?|dialogs?|modals?|menus?|toolbars?|sidebars?)"
_UI_PRODUCT_ANCHOR = (
    r"(?:login|logout|signup|sign[\s\-]?up|sign[\s\-]?in|settings|search|"
    r"filters?|checkout|profile|admin|landing|home|account|charts?|metrics|"
    r"edit|notes?|dashboards?|responsive|navigation|registration|billing|"
    r"contact|save|cancel|user)"
)
_UI_ARTIFACT_TAIL = (
    r"(?!\s+(?:helpers?|templates?|utils?|utilities|apis?|parsers?|"
    r"library|libraries|sdks?|packages?|validation|reference|"
    r"documentation|docs|examples?|objects?|fixtures?|locators?|models?|"
    r"errors?|failures?|warnings?|issues?|bugs?|urls?|findings?|counts?|"
    r"results?|outcomes?|screenshots?|snapshots?|"
    r"listed|returned|printed|logged|reported|exported|emitted|dumped|"
    r"catalogu?ed|indexed|recorded|detected|summarized|flagged|"
    r"and\s+(?:[\w\-]+\s+){0,3}?"
    r"(?:listed|returned|printed|logged|reported|exported|emitted|dumped)))"
)
# The front position is a denylist, not an allowlist (#1813 R9): a finite
# anchor list rejected ordinary product widgets ("password reset screen",
# "shopping cart page", "modal dialog"). Any modifier word is accepted
# except documentation/API-artifact vocabulary, which carries the
# documented library false positives.
_UI_DOC_MODIFIER = r"(?:documentation|docs|manual|wiki|readme|reference|examples?|api|help|errors?|exceptions?|crash|diagnostics?|audited|scanned|crawled|inspected)"
_UI_COMPOSITION_RE = re.compile(
    r"\b(?:"
    r"user\s+interface|"
    r"interactive\s+(?:charts?|dashboards?|pages?|forms?|panels?|navigation|ui)|"
    r"client[\s\-]side\s+validation\s+(?:messages?|errors?|feedback)|"
    rf"(?!{_UI_DOC_MODIFIER}\s)\w+\s+{_UI_WIDGET_NOUN}{_UI_ARTIFACT_TAIL}|"
    rf"{_UI_WIDGET_NOUN}\s+for\s+{_UI_PRODUCT_ANCHOR}|"
    rf"{_UI_WIDGET_NOUN}\s+(?:submit|submission|rendered|shown|displayed|clicked)|"
    r"users?\s+can\s+(?:add|edit|delete|create|update|remove|drag|click|"
    r"filter|search|browse|submit|save|cancel|manage|view)|"
    r"drag[\s\-]and[\s\-]drop|"
    r"navigation\s+bars?|data\s+tables?|"
    r"homepages?|responsive\s+layouts?|accessible\s+controls?"
    r")\b"
)


# Whole manifest/lockfile path tokens ("packages/web/package.json",
# "package-lock.json") and the token-bounded library word (#1813 R5).
_MANIFEST_TOKEN_RE = re.compile(r"\S*package(?:-lock)?\.json\S*")
_LIBRARY_PACKAGE_WORD_RE = re.compile(r"\bpackage\b(?!-)")


_GOAL_CONJUNCT_SPLIT_RE = re.compile(
    r"\s*(?:\band\b|\bplus\b|\bwith\b|\balongside\b|\bas\s+well\s+as\b|"
    r"\balong\s+with\b|\btogether\s+with\b|\bin\s+addition\s+to\b|"
    r"\bbut\s+also\b|\bor\b|&|[,;])\s*"
)
_GAME_CONJUNCT_VOCAB_RE = re.compile(rf"\b{_GAME_GOAL_SIGNAL_FRAGMENT}\b")


# "to <verb> X" is an action target; "to a/an/the X" is a plain PP and
# "in addition to an X" a coordinator (#1813 R34) — articles are excluded.
_INFINITIVE_TARGET_RE = re.compile(r"\bto\s+(?!be\b|an?\b|the\b)[\w\-]+\s+[^,.;]*")
_RELATIONAL_TARGET_RE = re.compile(
    r"\b(?:targeting|supporting|serving|powering|backing|aimed\s+at|"
    # Passive consumption reads the same through any preposition (#1813
    # R103): what is consumed in, loaded by, imported by, or called from
    # web apps is their component, not the app.
    r"used\s+(?:by|in|inside|within)|consumed\s+(?:by|in|inside|within)|"
    r"loaded\s+(?:by|in|into|inside|within|from)|"
    r"imported\s+(?:by|into|in|from)|called\s+(?:from|by)|"
    r"invoked\s+(?:by|from)|referenced\s+(?:by|from)|required\s+by|"
    r"embedded\s+(?:in|into|within|inside)|"
    r"nested\s+(?:in|within|inside)|contained\s+(?:in|within|inside)|"
    # A wrapper/container/shell owns the produced artifact while the web
    # product after AROUND is its consumed dependency (#1813 R119).
    r"(?:wrappers?|containers?|shells?|wrapped|wrapping|containerized)\s+around|"
    r"built\s+around|"
    # Active containment consumes its component (#1813 R59): an app that
    # "embeds a web browser" hosts the browser, it is not one.
    r"embeds?|embedding|contains?|containing|hosts?|hosting|"
    r"includes?|including|incorporates?|incorporating|houses?|housing|"
    r"integrat\w*\s+with|"
    r"compatible\s+with|used\s+with|works?\s+with|interopera\w*\s+with|"
    r"paired\s+with|hosted\s+(?:on|in|within|inside)|deployed\s+(?:on|to)|"
    # Execution hosts are consumers of the artifact (#1813 R44): a
    # companion that "runs inside web apps" is hosted BY them.
    r"runs?\s+(?:on|in|inside|within)|running\s+(?:on|in|inside|within)|"
    r"lives?\s+(?:in|inside|within)|living\s+(?:in|inside|within)|"
    r"served\s+(?:from|on)|published\s+(?:on|to)|"
    # Comparison and imitation name a reference artifact, not the produced
    # one (#1813 R44): "mimics a web app" builds something else.
    r"mimic(?:s|king|ked)?|emulat\w*|imitat\w*|resembl\w*|"
    r"mirrors?|mirroring|modell?ed\s+(?:on|after)|styled\s+(?:after|like)|"
    r"patterned\s+(?:on|after)|inspired\s+by|similar\s+to|akin\s+to|"
    r"comparable\s+to|reminiscent\s+of|looks?\s+like|looking\s+like|"
    r"feels?\s+like|feeling\s+like|behaves?\s+like|behaving\s+like|"
    r"acts?\s+like|acting\s+like|works?\s+like|working\s+like|"
    r"designed\s+for|intended\s+for(?:\s+use\s+(?:with|in|by))?|"
    r"meant\s+for|built\s+for|made\s+for|usable\s+by|"
    r"available\s+to|accessible\s+to|open\s+to|exposed\s+to|"
    r"syncs?\s+with|synchroniz\w*\s+with|connects?\s+(?:to|with)|"
    r"links?\s+(?:to|with)|talks?\s+to|communicates?\s+with|"
    r"marketed\s+to|sold\s+to|advertised\s+to|promoted\s+to|pitched\s+to|"
    r"used\s+alongside|installed\s+(?:into|in|on)|"
    r"bundled\s+(?:with|into)|packaged\s+(?:with|into)|shipped\s+(?:with|into)|"
    r"plugged\s+into|mounted\s+(?:in|on)|"
    r"for\s+use\s+(?:with|in|by)|for)\s+"
    # Coordinated consumer lists are consumed whole (#1813 R37): the web
    # item may sit anywhere in "mobile apps and web apps".
    r"(?:(?:an?|the|my|our|your)\s+)?(?:[\w\-'’]+(?:\s+[\w\-'’]+){0,2}?\s*(?:,|\band\b|\bor\b|\bas\s+well\s+as\b|\balong\s+with\b|\bplus\b|/)\s*){0,3}?"
    r"(?:an?\s+|the\s+)?(?:[\w\-'’]+\s+){0,2}?"
    r"(?:web[\s\-]?app(?:lication)?s?|webapps?|websites?|web\s+uis?|frontends?|"
    r"front[\s\-]ends?|single[\s\-]page\s+app(?:lication)?s?|spas?|pwas?|browsers?)\b"
    r"(?:\s*(?:,|\band\b|\bor\b|\bas\s+well\s+as\b|\balong\s+with\b|\bplus\b|/)\s*(?:(?:an?|the|my|our|your)\s+)?[\w\-'’]+(?:\s+[\w\-'’]+){0,2}){0,3}"
)


# A participial content clause names what the product DISPLAYS or
# INSPECTS, not what is produced (#1813 R100/R102): "desktop dashboard
# showing a list of web apps" shows web apps, and "a report analyzing
# web applications" studies them — neither is one. Attributive gerunds
# that premodify a head ("delivery tracking web app") are not in this
# class and keep their reading.
_PARTICIPIAL_CONTENT_CLAUSE_RE = re.compile(
    r"\b(?:showing|displaying|listing|presenting|visualizing|summarizing|"
    r"highlighting|cataloging|cataloguing|enumerating|analyzing|analysing|"
    r"comparing|evaluating|indexing|describing|auditing|reviewing|"
    r"inspecting|assessing|examining|benchmarking|ranking|surveying)\b.*$"
)


def _goal_artifact_head_is_web_app(goal_text: str) -> bool:
    """Ownership follows the goal's artifact head (#1813 R21): in
    "package delivery tracking web app" or "SDK documentation web app"
    the library tokens are attributive subject modifiers of the web-app
    head, so they carry no library ownership. Heads are compared by last
    position, since English modifiers precede their head."""
    if _goal_denies_web_app_artifact(goal_text):
        return False
    # Postnominal qualifiers are consulted on the UNSPLIT goal, BEFORE
    # clause stripping (#1813 R45/R47): conjunct splitting would discard
    # a comma-introduced dependent qualifier ("an admin portal,
    # accessible in the browser"), and the subject-clause/relational
    # strips would remove the very qualifier that carries ownership. The
    # anchored chain cannot cross a real coordination, so co-product
    # commas stay outside the phrase. Similarity modifiers are normalized
    # first so "browser-like" cannot qualify.
    postnominal_core = _WEB_SIMILARITY_MODIFIER_RE.sub(
        " ", _strip_negated_signals(goal_text, _WEB_APP_GOAL_SIGNAL_FRAGMENT)
    ).strip()
    if _POSTNOMINAL_BROWSER_QUALIFIER_RE.search(postnominal_core):
        return True
    # The head is judged on the goal's FIRST conjunct (#1813 R33): a web
    # phrase in a later coordinated conjunct is a co-product (the grant
    # handles it) and must not erase the first conjunct's own artifact
    # evidence.
    goal_text = _GOAL_CONJUNCT_SPLIT_RE.split(goal_text)[0]
    # Negated spans leave first (#1813 R31): clause stripping would
    # otherwise behead a coordinated denial at its first comma and leave
    # the surviving alternatives looking affirmative.
    core = _strip_negated_signals(goal_text, _WEB_APP_GOAL_SIGNAL_FRAGMENT)
    core = _strip_consumed_dependencies(_SUBJECT_CLAUSE_RE.sub(" ", core))
    # An implementation adjunct after the head ("in TypeScript", "using
    # React") is an adverbial, not a re-heading compound (#1813 R116) —
    # only bare noun continuation ("web app scaffolding CLI") re-heads.
    core = re.sub(r"\b(?:in|using|via|atop|on\s+top\s+of|through)\s+[^,.;]*", " ", core)
    # "to test/analyze/deploy a web app" names the target of another
    # artifact, not the produced one (#1813 R28), and participial
    # relations ("SDK targeting web apps", "package used by web apps")
    # name the consumer (#1813 R29).
    core = _INFINITIVE_TARGET_RE.sub(" ", core)
    core = _strip_consumer_relations(core)
    # Content participles ("showing a list of web apps") name displayed
    # content, not the produced artifact (#1813 R100) — the same
    # ownership normalization the browser-context path applies.
    core = _PARTICIPIAL_CONTENT_CLAUSE_RE.sub(" ", core)
    # A noun relation re-heads the phrase (#1813 R101): "a catalog of
    # web apps" produces the catalog — the web nouns are its subject
    # matter. Likewise a goal that opens with an inspection verb
    # ("Analyze web apps") studies its object rather than producing it.
    core = re.sub(r"\b(?:of|between|among|versus|vs\.?)\s+[^,.;]*", " ", core)
    core = re.sub(
        r"^\s*(?:analyz\w+|compar\w+|review\w*|audit\w*|evaluat\w+|"
        r"benchmark\w*|survey\w*|stud(?:y|ies|ying)|inspect\w*|assess\w*|"
        r"examin\w+|catalog\w*|index\w*|rank\w*)\b[^,.;]*",
        " ",
        core,
    )
    web_matches = list(_WEB_APP_ARTIFACT_PHRASE_RE.finditer(core))
    if not web_matches:
        # A browser-qualified UI head ("browser-based admin portal",
        # "browser admin console") is affirmative ownership too (#1813
        # R44). The qualifier is searched on the stripped core, so
        # negated, relational, and similarity browser mentions cannot
        # qualify a head.
        qualified = _BROWSER_QUALIFIED_UI_HEAD_RE.search(core)
        if qualified is None:
            return False
        web_matches = [qualified]
    # A head is final: trailing words after the last web phrase ("web app
    # scaffolding CLI", "website generator CLI") mean the web phrase
    # modifies another artifact (#1813 W1).
    if re.search(r"\w", core[web_matches[-1].end() :]):
        return False
    intent_matches = list(_LIBRARY_INTENT_RE.finditer(core))
    if not intent_matches:
        return True
    return web_matches[-1].end() > intent_matches[-1].end()


# An adjectival containment premodifier marks the web phrase as a
# component of its host, not a co-produced artifact (#1813 R111): "a
# native mobile app with an embedded web UI" ships one app.
_CONTAINED_WEB_PHRASE_RE = re.compile(
    rf"\b(?:embedded|built[\s\-]?in|integrated|inline|internal|bundled|"
    rf"in[\s\-]app|nested)\s+(?:[\w\-'’]+\s+){{0,2}}?"
    rf"{_WEB_APP_GOAL_SIGNAL_FRAGMENT}\b"
)


def _goal_has_web_co_product_conjunct(goal_text: str) -> bool:
    """True only for genuinely coordinated goals where a SEPARATE conjunct
    requests a web product by artifact phrase (#1813 R30) — a lone goal
    mentioning the web is not a co-product declaration."""
    # Relational consumers ("integrating/compatible/used with a web app")
    # are normalized away first, so a raw "with" split cannot promote a
    # consumer to a co-produced artifact (#1813 R31); denied spans are
    # stripped before splitting so the comma pieces of a coordinated
    # denial ("not a browser, web app, or frontend") cannot masquerade
    # as co-product conjuncts.
    goal_text = _strip_consumer_relations(goal_text)
    goal_text = _strip_negated_signals(goal_text, _WEB_APP_GOAL_SIGNAL_FRAGMENT)
    # Infinitive/PP action targets ("and upload the docs to our website",
    # "to scaffold and deploy a web app") are not product declarations
    # (#1813 W1). A containment-premodified web phrase ("an embedded web
    # UI") is its host's component, not a conjunct of its own (#1813
    # R111).
    goal_text = _INFINITIVE_TARGET_RE.sub(" ", goal_text)
    goal_text = _CONTAINED_WEB_PHRASE_RE.sub(" ", goal_text)
    pieces = _GOAL_CONJUNCT_SPLIT_RE.split(goal_text)
    if len(pieces) < 2:
        return False
    # Each piece is judged on its own head (#1813 R32): a web mention
    # that is the target of documentation/plugins/adapters ("with
    # documentation for web apps") is not a co-produced app.
    piece_cores = [_CONTENT_CLAUSE_RE.sub(" ", piece) for piece in pieces]

    def _piece_declares_web_product(core: str) -> bool:
        if _LIBRARY_INTENT_RE.search(core):
            return False
        matches = list(_WEB_APP_ARTIFACT_PHRASE_RE.finditer(core))
        if not matches:
            # A coordinated conjunct uses the SAME positive ownership
            # grammar as a standalone goal (#1813 R105): a browser/web-
            # qualified UI head ("a local web dashboard") declares a web
            # product in its own conjunct too, under the same finality
            # rule.
            qualified = _BROWSER_QUALIFIED_UI_HEAD_RE.search(core)
            if qualified is None:
                return False
            return not re.search(r"\w", core[qualified.end() :])
        # The web phrase must be the piece's own head ("an admin web
        # app"), not a modifier of something else ("broken website
        # links") — same finality rule as the goal head (#1813 W1).
        if re.search(r"\w", core[matches[-1].end() :]):
            return False
        return _goal_has_unnegated_web_app_signal(core)

    return any(_piece_declares_web_product(core) for core in piece_cores)


def _goal_has_web_only_conjunct(goal_text: str, other_evidence_re: re.Pattern[str]) -> bool:
    """True when some goal conjunct affirmatively requests a web app
    without carrying the other class's evidence (#1813 R20)."""
    return any(
        _WEB_APP_GOAL_SIGNAL_RE.search(conjunct) and not other_evidence_re.search(conjunct)
        for conjunct in _GOAL_CONJUNCT_SPLIT_RE.split(goal_text)
        if _goal_has_unnegated_web_app_signal(conjunct)
    )


# A for-phrase whose object carries a human relative clause ("for
# developers who build web apps") is audience end to end (#1813 R41) —
# structural, not another token window.
_AUDIENCE_CLAUSE_RE = re.compile(
    r"\bfor\s+[^,.;]*?\b(?:who|whose|whom|interested\s+in|"
    r"working\s+(?:with|on)|familiar\s+with)\b[^,.;]*"
    r"|\bwhose\s+(?:users?|customers?|teams?|developers?|audiences?)\b[^,.;]*"
    # "for teams that use web apps": a that-clause counts as audience only
    # with an activity verb, so content clauses keep product ownership
    # (#1813 R42).
    r"|\bfor\s+[^,.;]*?\bthat\s+"
    r"(?:use|uses|build|builds|develop|develops|run|runs|manage|manages|"
    r"maintain|maintains|deploy|deploys|ship|ships|operate|operates|"
    r"create|creates|work\s+(?:with|on))\b[^,.;]*"
)


# A for-phrase ending at a people noun is audience (#1813 R109): "for
# CLI users" and "for REST API operators" name who the product serves,
# not what it produces.
_PLAIN_AUDIENCE_PHRASE_RE = re.compile(
    r"\bfor\s+(?:[\w\-'’]+\s+){0,3}?(?:users?|operators?|developers?|"
    r"engineers?|admins?|administrators?|teams?|customers?|analysts?|"
    r"managers?|owners?|stakeholders?|people|staff|audiences?)\b"
)


def _strip_audience_and_displayed_subjects(text: str) -> str:
    """Remove audience phrases and displayed-subject participles — the
    vocabulary inside them describes who the product serves or what it
    shows, and owns nothing for any class (#1813 R109)."""
    text = _AUDIENCE_CLAUSE_RE.sub(" ", text)
    text = _PLAIN_AUDIENCE_PHRASE_RE.sub(" ", text)
    return _PARTICIPIAL_CONTENT_CLAUSE_RE.sub(" ", text)


# "browser-like", "web-app-style": a similarity suffix marks the web token
# as a comparison reference, not the produced artifact (#1813 R44).
def _strip_consumer_relations(text: str) -> str:
    """Remove relational consumer targets, human-audience clauses, and
    similarity/component modifiers — none of them name the produced
    artifact."""
    text = _WEB_SIMILARITY_MODIFIER_RE.sub(" ", text)
    text = _BROWSER_COMPONENT_RE.sub(" ", text)
    return _RELATIONAL_TARGET_RE.sub(" ", _AUDIENCE_CLAUSE_RE.sub(" ", text))


def _section_browser_context_text(ledger: SeedDraftLedger) -> str:
    """Normalized outputs/runtime text for browser-context decisions.

    Token- and negation-aware (#1813 R23): "Non-browser desktop runtime"
    and "No browser; local desktop runtime" are denials, not evidence.
    Relationship-aware like every other ownership surface (#1813 R36):
    interop/compatibility targets in outputs are consumers of the
    artifact, not evidence that it IS a browser product. The
    runtime_context section is exempt from consumer normalization
    (#1813 R56): it declares the execution environment, so "Runs in
    browsers" IS the affirmative evidence the relation pattern would
    otherwise erase; denials still route through the negation and
    postpositive strips.
    """
    outputs = _section_text(ledger, "outputs")
    # Verification-owned clauses are not the execution environment
    # (#1813 R100): in "Native desktop application; Playwright tests run
    # in browsers" the browser hosts the tests, not the product, so it
    # cannot serve as section browser evidence. A genuine browser
    # runtime clause survives the strip and keeps its authority.
    # Contained engines leave with the verification clauses (#1813
    # R110): a browser token that is the object of embeds/uses/bundles
    # is the host's bundled engine, not the environment.
    runtime = _ENGINE_CONTAINMENT_RE.sub(
        " ", _strip_verification_clauses(_section_text(ledger, "runtime_context"))
    )
    runtime = _RUNTIME_BROWSER_CONSUMER_RE.sub(" ", runtime)
    context_text = _strip_consumer_relations(outputs) + " " + runtime
    # Postpositive denials ("the browser is not supported", "browser
    # support disabled") negate from behind (#1813 R37).
    context_text = _POSTPOSITIVE_BROWSER_DENIAL_RE.sub(" ", context_text)
    return _strip_negated_signals(context_text, _WEB_APP_GOAL_SIGNAL_FRAGMENT)


def _ledger_has_browser_context(ledger: SeedDraftLedger) -> bool:
    """Affirmative browser context: standardized outputs/runtime keywords,
    or an unnegated goal-side web-app signal. Shared by the web_app
    matcher and by game_2d's ceding rule for render/screen vocabulary.

    Section evidence uses the strict environment reading on every path
    (#1813 R58): a re-headed or embedded browser phrase is a component
    of another artifact, whichever branch consults it.
    """
    # Goal-side evidence is scoped to affirmative declarations (#1813
    # R66): a browser mention that is the object of a manipulation verb
    # ("opens browser pages") or lives in a subject-matter clause
    # ("about browser performance") names a target or topic, not the
    # artifact's environment.
    goal_for_context = _strip_consumer_relations(_goal_text(ledger))
    goal_for_context = _MANIPULATED_TARGET_RE.sub(" ", goal_for_context)
    goal_for_context = _ABOUT_CLAUSE_RE.sub(" ", goal_for_context)
    # Relative clauses describe what the artifact DOES to its content
    # ("that lists websites"), not what it is (#1813 R67) — copular
    # clauses ("that is not just a browser page") state identity and
    # keep their signal; goals whose relative clause declares the
    # environment ("that runs in browsers") already own through the
    # postnominal grant before context is asked.
    goal_for_context = re.sub(
        r"\b(?:that|which)\s+"
        r"(?!(?:is|are|was|were|becomes?|remains?|stays?|isn|aren|wasn|weren)\b)"
        r"[^,.;]*",
        " ",
        goal_for_context,
    )
    # Topical predicates relate the artifact to its subject (#1813 R70):
    # "focused on browser crashes", "devoted to web app incident
    # reports" — an adjective/participle plus its preposition opens a
    # topic, and everything to the clause boundary is subject matter.
    goal_for_context = re.sub(
        r"\b(?:focused|focusing|centered|centering|centred|centring|"
        r"devoted|dedicated|sourced|based|oriented|concentrated|"
        r"concentrating|specialized|specializing|specialised|"
        r"specialising|revolving|concerned|dealing)\s+"
        r"(?:on|around|to|from|upon|in|with)\b[^,.;]*",
        " ",
        goal_for_context,
    )
    # A bare on/with-phrase is topical unless its object ENDS at a web
    # signal (#1813 R71/R72): "on browser crash reports" and "with
    # browser crash analytics" name subject matter, while "runs on
    # browsers", "compatible with modern browsers", and "with no errors
    # in the web UI" keep their environment/quality reading.
    goal_for_context = re.sub(
        rf"\b(?:on|upon|with)\s+"
        rf"(?![^,.;]*\b{_WEB_APP_GOAL_SIGNAL_FRAGMENT}\b\s*(?:[,.;]|$))[^,.;]*",
        " ",
        goal_for_context,
    )
    # A browser/web token re-headed by a subject/event or measurement
    # noun is subject matter (#1813 R69/R73): "browser usage metrics",
    # "browser crash reports", "web app incident dashboards" describe
    # what the artifact reports on.
    goal_for_context = re.sub(
        rf"\b{_WEB_APP_GOAL_SIGNAL_FRAGMENT}\s+{_SUBJECT_COMPOUND_NOUN_FRAGMENT}\b",
        " ",
        goal_for_context,
    )
    # Structurally, a bare browser token re-headed by a PLAIN noun is a
    # topic compound (#1813 R76) — no vocabulary required. UI heads
    # ("browser dashboard"), activity gerunds ("browser monitoring
    # dashboard"), deliberate hyphen compounds ("browser vector-scene
    # editor"), and the terminal "browser use" idiom keep their reading.
    # A qualifier ADJECTIVE premodifying a UI head is a spaced qualifier,
    # not a re-heading noun (#1813 R101): "browser compatible dashboard"
    # reads exactly like its hyphenated form. Adjectives are recognized
    # by morphology (-ible/-able) plus the closed qualifier set; a plain
    # NOUN before the head ("browser certificate dashboard") stays a
    # topic compound.
    goal_for_context = re.sub(
        rf"\bbrowsers?\s+(?!{_UI_PRODUCT_HEAD_FRAGMENT}\b)(?!use\b)"
        rf"(?![\w'’]+(?:ible|able)\s+{_UI_PRODUCT_HEAD_FRAGMENT}\b)"
        rf"(?!(?:ready|friendly|capable|aware|enabled|native)\s+"
        rf"{_UI_PRODUCT_HEAD_FRAGMENT}\b)"
        r"(?![\w'’]*ing\b)[\w'’]+(?![\w'’\-])",
        " ",
        goal_for_context,
    )
    # A participle taking an object names the artifact's data ("tracking
    # browser usage", "showing web app uptime"); one followed by a
    # preposition declares its environment ("running in browsers") and
    # stays (#1813 R68). Nominal gerund compounds ("landing page") are
    # product vocabulary, not clauses.
    goal_for_context = re.sub(
        rf"\b(?!{_NOMINAL_GERUND_FRAGMENT}\b)"
        r"[a-z'’\-]+ing\s+(?!(?:in|on|at|inside|within|across)\b)[^,.;]*",
        " ",
        goal_for_context,
    )
    if _SECTION_BROWSER_ENV_RE.search(_section_browser_context_text(ledger)):
        return True
    # The standardized runtime is the authority on environment (#1813
    # R74): when it declares a competing non-browser environment and no
    # browser token survives its own normalization, goal prose cannot
    # supply browser context — affirmative products still own through
    # the grants before context is consulted.
    runtime_env = _strip_negated_signals(
        _ENGINE_CONTAINMENT_RE.sub(
            " ",
            _POSTPOSITIVE_BROWSER_DENIAL_RE.sub(" ", _section_text(ledger, "runtime_context")),
        ),
        _WEB_APP_GOAL_SIGNAL_FRAGMENT,
    )
    if _NON_BROWSER_RUNTIME_RE.search(runtime_env) and not _SECTION_BROWSER_ENV_RE.search(
        runtime_env
    ):
        return False
    return _goal_has_unnegated_web_app_signal(goal_for_context)


# Ownership confidence tiers for the qualified-head grant (#1813 R76).

# Artifact-intent vocabulary — exactly the signals _matches_library treats
# as authoritative ("reusable" alone is not one, #1813 R12), and denials
# are stripped before either matcher consults the goal ("not a library"
# is not intent).
_LIBRARY_INTENT_RE = re.compile(
    r"\b(?:importable|librar(?:y|ies)|sdks?|api\s+surface|public\s+api|package(?!-))\b"
)
_LIBRARY_INTENT_FRAGMENT = (
    r"(?:librar(?:y|ies)|sdks?|packages?|api\s+surface|public\s+api|importable)"
)


def _library_visible_goal(ledger: SeedDraftLedger) -> str:
    """Goal text for the library matcher's own evidence: content-verb
    clauses are subject matter (#1813 R18/R19), denials are stripped
    (#1813 R12), consumed dependencies are not produced shape (#1813
    R25), and artifact-defining clauses survive."""
    # Audience clauses and displayed-subject participles leave first
    # (#1813 R109): "for teams who use an SDK" and "showing package
    # metadata" carry library vocabulary without producing a library —
    # artifact-defining clauses ("which exposes an SDK") are untouched.
    goal = _strip_audience_and_displayed_subjects(_goal_text(ledger))
    goal = _LIBRARY_SUBJECT_CLAUSE_RE.sub(" ", goal)
    goal = _strip_consumed_dependencies(goal)
    return _strip_negated_signals(goal, _LIBRARY_INTENT_FRAGMENT)


_INSPECTION_TOOL_GOAL_RE = re.compile(
    r"\b(?:automation|test(?:ing)?\s+suites?|tests?|crawlers?|scrapers?|"
    r"scanners?|spiders?|auditors?|audits?|audit\s+tools?|monitor(?:s|ing)?|"
    r"checkers?|linters?|analyzers?|validators?|profilers?|"
    r"end[\s\-]to[\s\-]end|e2e)\b"
)


# A UI surface declared FOR a component belongs to that component
# (#1813 R66) — the postfix mirror of the first-NP component rule. The
# trailing rejection keeps audiences ("for extension developers")
# outside the veto.
# IDENTITY relations re-head the surface unconditionally (#1813 R83):
# copulas, "ships as", "packaged/distributed as", bare "as" toward a
# determined NP.
_BROWSER_UI_PRODUCT_HEAD_RE = re.compile(
    r"\b(?:apps?|applications?|dashboards?|pages?|websites?|web\s+uis?|"
    r"user\s+interfaces?|frontends?)\b"
)
_PRODUCT_CONTENT_CLAUSE_RE = re.compile(
    r"\b(?:for|with|about|that|which|showing|displaying|containing|presenting)\b.*$"
)


def _goal_has_browser_ui_product_head(goal_text: str) -> bool:
    """True when browser context modifies the produced UI artifact.

    Inspection vocabulary can describe the product's subject rather than a
    separate tool ("browser monitoring dashboard", "dashboard showing test
    results").  The declaration ends before content/feature clauses, so its
    final UI head preserves product ownership across natural word order;
    inspection-suite/CLI/report heads still take the target-widget exclusion.
    """
    core = _PRODUCT_CONTENT_CLAUSE_RE.sub(" ", goal_text)
    matches = list(_BROWSER_UI_PRODUCT_HEAD_RE.finditer(core))
    if not matches or re.search(r"\w", core[matches[-1].end() :]):
        return False
    return _goal_has_unnegated_web_app_signal(core)


_MANIPULATED_TARGET_RE = re.compile(
    r"\b(?:opens?|opening|submits?|submitting|clicks?|clicking|fills?|filling|"
    r"screenshots?|captures?|capturing|crawls?|crawling|scrapes?|scraping|"
    r"visits?|visiting|navigates?|navigating|audits?|auditing|inspects?|"
    r"inspecting|aggregat\w*|pars\w*|extracts?|extracting|scans?|scanning|"
    r"verif\w*|checks?|checking|asserts?|asserting)\s+"
    r"(?:an?\s+|the\s+)?(?:[\w\-'’]+\s+){0,2}?"
    r"(?:forms?|pages?|panels?|buttons?|screens?|dialogs?|modals?|menus?)\b"
    # Coordinated targets are manipulated whole (#1813 R116):
    # "aggregated login pages and signup forms" strips both conjuncts.
    r"(?:\s*(?:,|\band\b|\bor\b)\s*(?:an?\s+|the\s+)?(?:[\w\-'’]+\s+){0,2}?"
    r"(?:forms?|pages?|panels?|buttons?|screens?|dialogs?|modals?|menus?)\b){0,3}"
)


_UI_SIGNAL_STRIP_FRAGMENT = (
    r"(?:user\s+interface|forms?|panels?|buttons?|dashboards?|pages?|"
    r"screens?|dialogs?|modals?|menus?|toolbars?|sidebars?)"
)


_VERIFICATION_CONTEXT_RE = re.compile(
    r"\b(?:tests?|testing|smoke|e2e|end[\s\-]to[\s\-]end|playwright|"
    r"selenium|puppeteer|cypress|webdriver|qa|verification|"
    r"compatibility|automation|drivers?)\b"
)

# A hyphenated verification compound premodifies its head without owning
# it ("Playwright-tested extension runtime") — the compound token drops,
# the identity it modifies stays.
_VERIFICATION_COMPOUND_RE = re.compile(
    r"\b[\w'’]+-(?:tested|verified|validated|checked|covered|driven|exercised)\b"
)

# Introducers that attach a verification description to a preceding
# production identity: participial modifiers, relativizers, and the
# prepositions/markers that govern a tooling phrase.
_VERIFICATION_ATTACHMENT_CUT_RE = re.compile(
    r"\b(?:that|which|tested|verified|validated|checked|exercised|covered|"
    r"driven|with|via|using|by|for|to|under|through|alongside)\b"
)

# A described noun phrase declares the artifact's identity even inside
# verification prose ("tests cover the production Chrome browser
# extension runtime") — the declaration survives the exemption. The
# distinction is structural, not an ownership vocabulary (#1813
# R92-R97): a component that carries a description of its own — any
# postnominal predication (participial, prepositional, or a bare finite
# relative clause) or a prenominal state/role qualifier — is a produced
# artifact by default. What removes it is the exception classes the
# tooling owns: verification vocabulary, fixture words ("mock",
# "fixtures"), and transience markers ("installed temporarily"). A bare
# tooling object ("tests with a browser extension") carries no
# description and owns nothing.
_COMPONENT_NP_SPAN_RE = re.compile(
    rf"(?:(?:the|an?|our|its|this|that)\s+)?(?P<premods>(?:[\w\-'’]+\s+){{0,3}}?)"
    rf"(?:{_COMPONENT_ARTIFACT_FRAGMENT})\b(?P<window>(?:\s+[\w\-'’]+){{0,7}})"
)

_PRENOMINAL_STATE_QUALIFIER_RE = re.compile(
    r"\b(?:production|shipped|deployed|released|live|[\w'’]+-[\w'’]+(?:ing|ed))\b"
)

_FIXTURE_MARKER_RE = re.compile(
    r"\b(?:mocks?|mocked|stubs?|stubbed|fakes?|faked|dumm(?:y|ies)|"
    r"placeholders?|fixtures?|scaffold\w*|sandbox\w*|throwaway|disposable|"
    r"synthetic|simulated|samples?|temporar\w*|briefly|momentarily|"
    r"transient\w*|ephemeral|short[\s\-]lived|provisional|interim|scratch)\b"
)

# The harness owns what it creates and recycles (#1813 R98): a component
# the tooling generates, resets, or stands up per scenario/run/case is a
# verification target, not the produced artifact.
_HARNESS_LIFECYCLE_RE = re.compile(
    r"\b(?:generated|regenerated|created|recreated|rebuilt|reset|"
    r"respawned|instantiated|discarded|disposed|replaced|swapped|"
    r"refreshed|spun\s+up|torn\s+down|"
    r"(?:every|each|per)\s+(?:scenarios?|tests?|runs?|cases?|iterations?|"
    r"sessions?|suites?))\b"
)

# A component that is the SUBJECT of a possession or provision verb owns
# what follows (#1813 R99): "extension includes a QA feedback panel"
# describes the extension's own feature — the QA vocabulary is
# possessed, not possessing.
_COMPONENT_POSSESSION_RE = re.compile(
    r"^\s*(?:(?:that|which)\s+)?(?:includes?|including|contains?|containing|"
    r"provides?|providing|offers?|offering|features?|featuring|embeds?|"
    r"embedding|exposes?|exposing|adds?|adding|bundles?|bundling|ships?|"
    r"shipping|comes?|coming|carries?|carrying|hosts?|hosting|renders?|"
    r"rendering|displays?|displaying|supports?|supporting|integrates?|"
    r"integrating)\b"
)

# Retention taint: a candidate identity phrase whose own words belong to
# verification prose — a verification participle or a CI locus — is the
# tooling's description, not the artifact's ("extension tested nightly",
# "extensions in CI").
_VERIFICATION_TAINT_RE = re.compile(
    r"\b(?:tested|verified|validated|checked|exercised|covered|ci)\b"
)


def _strip_verification_clauses(text: str) -> str:
    """Remove verification-owned prose while keeping production identity.

    Scoped per relation, not per punctuation block (#1813 R91): a
    segment mixing both — "a Chrome browser extension and Playwright
    tests run in CI", the comma form, or "extension runtime tested with
    Playwright" — keeps the production identity and sheds only the
    clause or attachment the verification vocabulary owns. A clause
    with no attachment boundary before its first verification word is
    verification prose outright and drops whole — except for a
    production-marked noun phrase inside it (#1813 R92), which declares
    the artifact's identity in either clause order.
    """
    kept: list[str] = []
    for segment in re.split(r"[.;]", text):
        normalized = _VERIFICATION_COMPOUND_RE.sub(" ", segment)
        if not _VERIFICATION_CONTEXT_RE.search(normalized):
            kept.append(normalized)
            continue
        for part in re.split(r",|\b(?:and|while|but|whereas|or)\b", normalized):
            if not _VERIFICATION_CONTEXT_RE.search(part):
                kept.append(part)
                continue
            # The retained prefix ends at the last attachment introducer
            # before the clause's first verification word — everything
            # after it is the verification description.
            prefix = ""
            for cut in _VERIFICATION_ATTACHMENT_CUT_RE.finditer(part):
                candidate = part[: cut.start()]
                if _VERIFICATION_CONTEXT_RE.search(candidate):
                    break
                if re.search(r"\w", candidate):
                    prefix = candidate
            if prefix:
                kept.append(prefix)
            for np_match in _COMPONENT_NP_SPAN_RE.finditer(part[len(prefix) :]):
                phrase = np_match.group(0)
                if _FIXTURE_MARKER_RE.search(phrase) or _HARNESS_LIFECYCLE_RE.search(phrase):
                    continue
                # A production-marked component owns its description even
                # when that description mentions testing ("Production …
                # extension includes test diagnostics") — the component
                # possesses the diagnostics; the tooling does not possess
                # the component (#1813 R98). The same holds when the
                # component is the subject of a possession verb (#1813
                # R99): "extension includes a QA feedback panel" is the
                # extension's own feature.
                production_marked = bool(
                    _PRENOMINAL_STATE_QUALIFIER_RE.search(np_match.group("premods"))
                )
                window = np_match.group("window")
                possesses = bool(_COMPONENT_POSSESSION_RE.search(window))
                if not (bool(window.strip()) or production_marked):
                    continue
                # Ownership is decided over the component's COMPLETE
                # relation (#1813 R99): a fixture or lifecycle marker
                # anywhere in the segment disowns an unmarked component
                # even when a conjunction separates them.
                segment_owned_by_harness = bool(
                    _FIXTURE_MARKER_RE.search(normalized)
                    or _HARNESS_LIFECYCLE_RE.search(normalized)
                )
                if (
                    production_marked
                    or possesses
                    or (
                        not segment_owned_by_harness
                        and not _VERIFICATION_CONTEXT_RE.search(phrase)
                        and not _VERIFICATION_TAINT_RE.search(phrase)
                    )
                ):
                    kept.append(phrase)
    return " ; ".join(kept)


def _matches_web_app(ledger: SeedDraftLedger) -> bool:
    outputs = _section_text(ledger, "outputs")
    runtime = _section_text(ledger, "runtime_context")
    if not (outputs or runtime):
        # Same ledger-evidence gate as cli: goal text alone cannot classify.
        return False
    # Artifact intent voids app UI evidence wherever _matches_library
    # treats it as authoritative — outputs ("Reusable modal dialogs") and
    # goal ("Build a frontend SDK") alike (#1813 R10/R11) — and
    # game-domain ledgers own their shared render/screen vocabulary, the
    # mirror of _matches_game_2d's ceding rule.
    # An artifact-type denial in the goal governs the whole ledger (#1813
    # R15): outputs naturally described with browser wording ("Browser
    # accessibility report") cannot revive the denied classification.
    if _goal_denies_web_app_artifact(_goal_text(ledger)):
        return False
    # Goal-side postpositive exclusions precede every grant (#1813 R77),
    # scoped by content clauses like the artifact denials (#1813 R78): a
    # denial the app DISPLAYS ("showing which browsers are unsupported")
    # is content, not scope.
    goal_text_value = _goal_text(ledger)
    # Subordinate and participial content clauses scope a denial to what
    # they modify (#1813 R79/R80): "where embedded browsers are
    # disabled" belongs to its conjunct, "displaying websites that are
    # unavailable" is displayed content. A prepositional tail after the
    # denial names its own scope object ("unsupported for the desktop
    # client") — closed function words, not an object list.
    content_regions = (
        [m.span() for m in _CONTENT_CLAUSE_RE.finditer(goal_text_value)]
        + [m.span() for m in re.finditer(r"\bwhere\s+[^,.;]*", goal_text_value)]
        + [
            m.span()
            for m in re.finditer(
                rf"\b(?!{_NOMINAL_GERUND_FRAGMENT}\b)[a-z'’\-]+ing\s+[^,.;]*",
                goal_text_value,
            )
        ]
    )
    if any(
        not any(start <= m.start() < end for start, end in content_regions)
        and not re.match(
            r"\s+(?:when|if|unless|while|whenever|wherever|until|before|"
            r"after|without|because|since|due|owing|thanks)\b",
            goal_text_value[m.end() :],
        )
        and not re.match(
            r"\s+(?:from|in|within|inside|for|on|by|under|across|during|"
            r"except|only|outside|to)\b"
            # A condition/time qualifier is not a scope OBJECT (#1813
            # R81): "in production" and "for now" exclude globally.
            r"(?!\s+(?:(?:the|this|that)\s+)?(?:production|staging|"
            r"development|deployment|ci|now|meantime|moment|present|"
            r"future|foreseeable\s+future|near\s+term|time\s+being|"
            r"releases?|versions?|phases?|milestones?|sprints?|"
            r"iterations?)\b)",
            goal_text_value[m.end() :],
        )
        for m in _POSTPOSITIVE_BROWSER_DENIAL_RE.finditer(goal_text_value)
    ):
        return False
    # Goal-side component identity resolves before every affirmative
    # grant (#1813 R80). Identity relations re-head unconditionally;
    # association relations spare an explicit web artifact named before
    # them — an independent co-product, audience, or content owner
    # (#1813 R83).
    if _GOAL_COMPONENT_IDENTITY_RE.search(goal_text_value):
        return False
    # For the component rule, only that/which and participial clauses
    # are content regions — a for/about clause IS the association being
    # judged (#1813 R85).
    component_content_regions = [
        m.span() for m in re.finditer(r"\b(?:that|which)\s+[^,.;]*", goal_text_value)
    ] + [
        m.span()
        for m in re.finditer(
            rf"\b(?!{_NOMINAL_GERUND_FRAGMENT}\b)[a-z'’\-]+ing\s+[^,.;]*",
            goal_text_value,
        )
    ]
    if any(
        not _EXPLICIT_WEB_VOCAB_RE.search(goal_text_value[: m.start()])
        and not (
            # A bare-preposition association inside a content clause is
            # displayed/managed content (#1813 R85): "showing the status
            # OF browser extensions". A relation-participle start
            # ("belonging to ...") stays an ownership relation.
            not re.match(r"[a-z'’\-]+(?:ed|ing)\b", m.group(0))
            and any(start <= m.start() < end for start, end in component_content_regions)
        )
        for m in _GOAL_COMPONENT_TARGET_RE.finditer(goal_text_value)
    ):
        return False
    # An explicit component runtime is authoritative (#1813 R77) — as an
    # IDENTITY (#1813 R78/R79): negated mentions do not count, and the
    # decision is order-independent — the component owns only when NO
    # browser/web identity survives outside its own noun phrase, so a
    # two-product runtime keeps web ownership whichever side is named
    # first.
    component_fragment = _COMPONENT_ARTIFACT_FRAGMENT
    runtime_identity = _strip_negated_signals(
        _section_text(ledger, "runtime_context"), component_fragment
    )
    # A verification-context runtime is not the artifact's identity
    # when the goal explicitly names a web product (#1813 R88/R89):
    # "Playwright tests with a browser extension" and "Compatibility
    # tests for Chrome extensions" describe how the app is verified —
    # the components there are used or targeted by the tooling.
    if _goal_artifact_head_is_web_app(_goal_text(ledger)):
        # The exemption follows every affirmative web-product ownership
        # path (#1813 R95) — explicit vocabulary, qualified heads, and
        # postnominal browser qualifiers alike — and is scoped per
        # relation (#1813 R90/R91): only the prose the verification
        # vocabulary syntactically owns is exempt, while a production
        # identity sharing a segment through a conjunction, comma, or
        # participial attachment keeps its authority.
        runtime_identity = _strip_verification_clauses(runtime_identity)
    if re.search(rf"\b{component_fragment}\b", runtime_identity):
        residual = re.sub(
            rf"\b(?:[\w\-'’]+\s+){{0,2}}?{component_fragment}\b", " ", runtime_identity
        )
        if not _WEB_APP_GOAL_SIGNAL_RE.search(residual):
            return False
    # An affirmative web-product request owns its output description
    # (#1813 R27): when the goal's artifact head is a web app, the
    # standardized outputs describe that requested product, and no widget
    # catalog is required. Non-web-product goals (extensions, crawlers,
    # test suites, CLIs) still need genuine UI-composition evidence.
    if _goal_artifact_head_is_web_app(_goal_text(ledger)):
        # Qualified-head ownership is tiered by confidence (#1813
        # R75/R76). Explicit web artifact vocabulary owns outright. A
        # bare browser qualifier is the AMBIGUOUS form: it defers to a
        # competing runtime, owns when directly adjacent to its head or
        # when the standardized sections confirm a browser environment,
        # and with an intervening modifier it owns only an
        # interaction-class head ("vector-scene editor" acts on scenes;
        # a display-class "certificate dashboard" naturally takes
        # topics, so it needs section confirmation).
        goal_text_raw = _goal_text(ledger)
        runtime_env = _strip_negated_signals(
            _ENGINE_CONTAINMENT_RE.sub(" ", _POSTPOSITIVE_BROWSER_DENIAL_RE.sub(" ", runtime)),
            _WEB_APP_GOAL_SIGNAL_FRAGMENT,
        )
        runtime_bars_browser = bool(
            _NON_BROWSER_RUNTIME_RE.search(runtime_env)
        ) and not _SECTION_BROWSER_ENV_RE.search(runtime_env)
        if _EXPLICIT_WEB_VOCAB_RE.search(goal_text_raw):
            # Bare "frontend" is not inherently browser-hosted (#1813
            # R115): a native app's frontend is native. It owns outright
            # only alongside stronger web vocabulary or when no
            # authoritative non-browser runtime competes.
            if (
                _EXPLICIT_WEB_VOCAB_RE.search(re.sub(r"\bfront[\s\-]?ends?\b", " ", goal_text_raw))
                or not runtime_bars_browser
            ):
                return True
        if not runtime_bars_browser:
            if (
                _BARE_ADJACENT_QUALIFIER_RE.search(goal_text_raw)
                # A postnominal qualifier states the environment in the
                # goal's own words ("that runs in browsers") — it owns
                # like an adjacent qualifier does (#1813 R95), without
                # waiting for section confirmation.
                or _POSTNOMINAL_BROWSER_QUALIFIER_RE.search(goal_text_raw)
                or _SECTION_BROWSER_ENV_RE.search(_section_browser_context_text(ledger))
                or _INTERACTION_HEAD_TAIL_RE.search(re.split(r"[.;:!?,]", goal_text_raw)[0])
            ):
                return True
    # An explicitly co-produced web app ("a Python library with an admin
    # web app") retains web ownership regardless of incidental output
    # wording (#1813 R30) — the overlap stays an honest ambiguity.
    if _goal_has_web_co_product_conjunct(_goal_text(ledger)):
        return True
    # A later UI-headed conjunct takes the same browser-runtime
    # corroboration as the first noun phrase (#1813 R106): "a backend
    # with an admin dashboard" running against browsers keeps the
    # dashboard's web ownership as an honest ambiguity. Negative
    # coordinators surrender the conjunct before splitting.
    conjunct_goal = re.sub(
        r"\b(?:without|minus|excluding|except)\b[^,.;]*", " ", _goal_text(ledger)
    )
    if any(
        _goal_first_np_is_ui_headed(conjunct)
        for conjunct in _GOAL_CONJUNCT_SPLIT_RE.split(conjunct_goal)[1:]
    ) and _SECTION_BROWSER_ENV_RE.search(_section_browser_context_text(ledger)):
        return True
    # Subject/secondary clauses in the goal ("for game leaderboards",
    # "with a public API") name what the app is about or co-produces, not
    # its artifact shape — they do not surrender ownership (#1813 R16).
    # Library intent is authoritative on the goal side only (#1813 R17):
    # outputs enumerate every deliverable, so an API/SDK co-produced next
    # to an affirmative browser UI stays an honest multi-class question
    # for _matches_library rather than a web_app veto.
    # Cross-class vetoes are conjunct-aware (#1813 R20): a goal that
    # explicitly requests a web app in its own conjunct ("... and a
    # separate admin web app") keeps independent web ownership, and the
    # overlap becomes an honest ambiguity instead of a veto.
    # Bare displayed-subject participles are subject matter too (#1813
    # R109): "showing package metadata" cannot veto the spreadsheet that
    # shows it.
    guard_goal = _strip_consumed_dependencies(
        _PARTICIPIAL_CONTENT_CLAUSE_RE.sub(" ", _SUBJECT_CLAUSE_RE.sub(" ", _goal_text(ledger)))
    )
    intent_text = _MANIFEST_TOKEN_RE.sub(
        " ", _strip_negated_signals(guard_goal, _LIBRARY_INTENT_FRAGMENT)
    )
    if (
        _LIBRARY_INTENT_RE.search(intent_text)
        and not _goal_has_web_only_conjunct(_goal_text(ledger), _LIBRARY_INTENT_RE)
        and not _goal_artifact_head_is_web_app(_goal_text(ledger))
    ):
        return False
    # Game vocabulary suppresses web_app only when the game predicate has
    # artifact-shape evidence of its own (#1813 R17) — subject words like
    # "game leaderboard" in outputs are not a rendered game.
    if _matches_game_2d(ledger) and not _goal_has_web_only_conjunct(
        _goal_text(ledger), _GAME_CONJUNCT_VOCAB_RE
    ):
        return False
    # Two-signal AND (the webhook shape): browser context plus UI
    # composition. Goal-side browser mentions route through the negation
    # strip so "not a browser page" is not positive evidence. UI
    # composition is required from standardized ledger outputs ONLY (#1813
    # R2): goal prose mentions UI vocabulary in denials ("without forms or
    # pages") and domain references ("browser form parsing"), neither of
    # which asserts that the artifact HAS a UI.
    # Output-side UI denials count too (#1813 R21): "No user interface"
    # is an explicit statement that the artifact has none. Widgets of an
    # inspected or manipulated TARGET ("submits login forms",
    # "screenshots login pages", "aggregated login page metadata") are
    # not UI the artifact produces (#1813 R26).
    # A goal declaring an inspection/automation artifact marks output
    # widgets as its targets (#1813 R30) — clause-level ownership, not
    # another noun list.
    # The product-head exception accepts standardized runtime evidence
    # (#1813 R61): "webhook monitoring dashboard" with a browser
    # execution environment is a dashboard whose subject is monitoring,
    # not a monitoring tool.
    if _INSPECTION_TOOL_GOAL_RE.search(_goal_text(ledger)) and not (
        _goal_has_browser_ui_product_head(_goal_text(ledger))
        or (
            # The escape from the inspection guard demands an
            # AFFIRMATIVE UI product head (#1813 R61/R89) — a testing
            # framework or runner under a browser runtime is still a
            # tool, while a monitoring/testing DASHBOARD is a product.
            _goal_first_np_is_ui_headed(_goal_text(ledger))
            and _SECTION_BROWSER_ENV_RE.search(_section_browser_context_text(ledger))
        )
    ):
        return False
    # Positive UI-product ownership precedes output composition (#1813
    # R55): the goal's first noun phrase must carry UI vocabulary before
    # widget-rich outputs may corroborate — "a report on browser pages"
    # keeps its report identity regardless of output wording.
    if not _goal_first_np_has_ui_shape(_goal_text(ledger)):
        return False
    ui_text = _strip_negated_signals(outputs, _UI_SIGNAL_STRIP_FRAGMENT)
    ui_text = _MANIPULATED_TARGET_RE.sub(" ", ui_text)
    if _ledger_has_browser_context(ledger) and _UI_COMPOSITION_RE.search(ui_text):
        return True
    # An affirmative UI-product head plus a standardized browser
    # execution environment is semantic UI evidence (#1813 R57):
    # ordinary user-flow outputs ("users authenticate, manage roles")
    # need no widget vocabulary. The section token must be the
    # environment itself — a re-headed browser artifact ("browser
    # extension runtime") does not qualify. The fallback honors the same
    # runtime authority as the tiered grants (#1813 R108): an
    # affirmative non-browser host keeps ownership over engine mentions
    # riding along in its own description.
    fallback_runtime_env = _strip_negated_signals(
        _ENGINE_CONTAINMENT_RE.sub(" ", _POSTPOSITIVE_BROWSER_DENIAL_RE.sub(" ", runtime)),
        _WEB_APP_GOAL_SIGNAL_FRAGMENT,
    )
    if _NON_BROWSER_RUNTIME_RE.search(fallback_runtime_env) and not _SECTION_BROWSER_ENV_RE.search(
        _strip_verification_clauses(fallback_runtime_env)
    ):
        return False
    return bool(_SECTION_BROWSER_ENV_RE.search(_section_browser_context_text(ledger)))


_CONSUMED_DEPENDENCY_RE = re.compile(
    r"\b(?:to|from|against|via|using|through|clients?\s+for|consumers?\s+for|"
    r"consumes?|consuming|calls?|calling|"
    r"powered\s+by|backed\s+by|built\s+on|driven\s+by|served\s+by|"
    r"integrat\w*\s+with|invokes?|invoking|fetch\w*\s+(?:data\s+)?from|"
    r"uses|dependent\s+(?:on|upon)|depends?\s+(?:on|upon)|relies?\s+on|"
    r"wrapp?\w*|built\s+around|compatible\s+with|works?\s+with|"
    r"interopera\w*\s+with|paired\s+with)\s+"
    r"(?!(?:be|expose|exposing|provide|providing|offer|offering|serve|serving|"
    r"publish|publishing|host|hosting|implement|implementing|build|building|"
    r"create|creating|deliver|delivering)\b)"
    r"(?:an?\s+|the\s+)?(?:[\w\-'’]+\s+){0,2}?"
    r"(?:public\s+api|rest\s+apis?|apis?|sdks?|clis?|command[\s\-]line(?:\s+(?!(?:or|and|nor|but)\b)[\w\-'’]+){0,2}|web\s+services?)\b"
    # Later coordinated items are dependencies too (#1813 R37).
    r"(?:\s*(?:,|\band\b|\bor\b|\bas\s+well\s+as\b|\balong\s+with\b|\bplus\b|/)\s*(?:(?:an?|the|my|our|your)\s+)?[\w\-'’]+(?:\s+[\w\-'’]+){0,2}){0,3}"
)
_PRENOMINAL_DEPENDENCY_CLIENT_RE = re.compile(
    r"\b(?:public\s+api|rest\s+apis?|apis?|sdks?|clis?|"
    r"command[\s\-]line|web\s+services?)\s+clients?\b"
)


# Destination ownership is structural (#1813 R52/R53): a segment-initial
# imperative transforming an EXISTING artifact ("adapt the existing
# script to a CLI", "migrate from a legacy script to a CLI") produces
# its "to" artifact — no verb enumeration. Three markers carry the
# distinction: the governing verb heads its segment; it is not a
# production verb (production verbs CREATE their object, so their "to"
# phrases stay relations of that object — "build a bridge to a REST
# API"); and its object is definite or source-marked, because
# transformation references something that already exists. Transfer
# statements with indefinite objects ("submits credentials to a public
# API") keep their dependency reading.
_PRODUCTION_VERB_FRAGMENT = (
    r"(?:build|builds|create|creates|make|makes|develop|develops|"
    r"implement|implements|design|designs|write|writes|produce|produces|"
    r"deliver|delivers|ship|ships|craft|crafts|construct|constructs|"
    r"add|adds|expose|exposes|provide|provides|offer|offers|publish|"
    r"publishes|host|hosts)"
)
_DESTINATION_CONTEXT_RE = re.compile(
    r"^\s*(?:(?:i|we|you|please|kindly|help|me|us|let[’']?s|want|wants|"
    r"wanted|need|needs|needed|would|like|to|going|plan|planning|aim|"
    r"aiming|intend|intending|hope|hoping|trying|try)\s+){0,6}?"
    rf"(?!{_PRODUCTION_VERB_FRAGMENT}\b)[a-z][\w\-]*\s+"
    # The object must be age/source-marked or anaphoric (#1813 R55):
    # transformation references something that already exists ("the
    # existing script", "from a legacy script", "it"), while transfers
    # take plainly definite or indefinite objects ("the credentials",
    # "a request") and keep their dependency reading. Bare definiteness
    # on either side decides nothing — "to the CLI described in the
    # requirements" is still a produced destination.
    r"(?=(?:[\w\-'’]+\s+){0,5}?(?:it|them|from|existing|legacy|old|"
    r"current|original|outdated|deprecated|previous|former)\b)"
    r"(?:[\w\-'’]+\s+){0,6}$"
)


# A relative clause whose subject is a UI artifact names what the UI
# acts on (#1813 R113): "login form that communicates with a REST API"
# consumes the API through WHATEVER verb — the relation is structural,
# not a verb list. Production verbs and copulas are the exception: a
# dashboard that EXPOSES a REST API produces it.
_UI_ACTS_ON_DEPENDENCY_RE = re.compile(
    rf"(?P<head>{_UI_PRODUCT_HEAD_FRAGMENT})\s+(?:that|which)\s+"
    rf"(?!(?:exposes?|exposing|provides?|providing|offers?|offering|"
    rf"serves?|serving|publishes?|publishing|hosts?|hosting|implements?|"
    rf"implementing|builds?|building|creates?|creating|delivers?|"
    rf"delivering|is|are|was|were|becomes?|exports?|exporting|produces?|"
    rf"producing|generates?|generating)\b)"
    rf"(?:[\w\-'’]+\s+){{1,4}}?"
    rf"(?:an?\s+|the\s+)?(?:[\w\-'’]+\s+){{0,2}}?"
    rf"(?:public\s+api|rest\s+apis?|graphql\s+apis?|apis?|sdks?|clis?|"
    rf"web\s+services?)\b"
)


def _strip_consumed_dependencies(text: str) -> str:
    """Remove relational and prenominal dependency/client declarations."""

    def _spare_destinations(match: re.Match[str]) -> str:
        if re.match(r"to\s", match.group(0)) is None:
            return " "
        segment = re.split(r"[.;:!?]", text[: match.start()])[-1]
        if _DESTINATION_CONTEXT_RE.match(segment):
            return match.group(0)
        return " "

    text = _UI_ACTS_ON_DEPENDENCY_RE.sub(lambda m: m.group("head") + " ", text)
    text = _CONSUMED_DEPENDENCY_RE.sub(_spare_destinations, text)
    return _PRENOMINAL_DEPENDENCY_CLIENT_RE.sub(" ", text)


def _matches_library(ledger: SeedDraftLedger) -> bool:
    outputs = _section_text(ledger, "outputs")
    goal = _goal_text(ledger)
    if not (outputs or goal):
        return False
    # "module" intentionally omitted — it is a generic Python term used
    # for any code unit (including CLI entry points) and produced too
    # many false positives that shadowed cli / web_service inference
    # under ledger_only closures. The remaining keywords are
    # library-distinctive surface terms. See #1170 R2 evidence.
    #
    # Manifest and lockfile tokens are masked whole before keyword matching
    # (#1813 R5): every browser project ships them, and a monorepo path like
    # `packages/web/package.json` would otherwise leave a "package"
    # substring behind. The library word itself is token-bounded so the
    # directory word "packages" is not mistaken for it, while the singular
    # "package" keeps its library meaning.
    # Goal tokens contribute no library ownership when the goal's artifact
    # head is a web app (#1813 R21) — they are attributive subject words.
    goal_evidence = "" if _goal_artifact_head_is_web_app(goal) else _library_visible_goal(ledger)
    # An API/SDK the artifact CONSUMES ("submits credentials to a public
    # API", "tokens from the payments SDK") is a dependency, not produced
    # library shape (#1813 R23).
    produced_outputs = _strip_consumed_dependencies(outputs)
    # A library token premodifying a UI head is the app's displayed
    # subject matter, not a produced surface (#1813 R106/R107): "package
    # search form" and "SDK authentication token status dashboard"
    # render content ABOUT packages and SDKs. The premodifier chain is
    # structural — any length, broken only by function words — so
    # "package with a CLI client" keeps its library reading.
    produced_outputs = re.sub(
        rf"\b(?:packages?|apis?|sdks?|librar(?:y|ies))\s+"
        rf"(?:(?!(?:with|and|or|for|plus|via|from|to|in|on)\b)[\w\-'’]+\s+)*?"
        rf"{_UI_PRODUCT_HEAD_FRAGMENT}\b",
        " ",
        produced_outputs,
    )
    text = _MANIFEST_TOKEN_RE.sub(" ", produced_outputs + " " + goal_evidence)
    return bool(_LIBRARY_PACKAGE_WORD_RE.search(text)) or _any_of(
        text,
        (
            "library",
            "api surface",
            "importable",
            "public api",
            "sdk",
        ),
    )


_PATTERN_REGISTRY: dict[TaskClass, _PatternFn] = {
    TaskClass.CLI: _matches_cli,
    TaskClass.WEBHOOK: _matches_webhook,
    TaskClass.WEB_SERVICE: _matches_web_service,
    TaskClass.WEB_APP: _matches_web_app,
    TaskClass.DATA_PIPELINE: _matches_data_pipeline,
    TaskClass.GAME_2D: _matches_game_2d,
    TaskClass.REFACTOR_IN_PLACE: _matches_refactor_in_place,
    TaskClass.LIBRARY: _matches_library,
}


def register_pattern(task_class: TaskClass, pattern_fn: _PatternFn) -> None:
    """Register a pattern function for *task_class*.

    Intended for tests and future extension PRs that add a new
    :class:`TaskClass` value. Production code should not call this — the
    static :data:`_PATTERN_REGISTRY` covers the 8-class catalog
    (#1173 plus ``web_app`` from #1813).
    """
    _PATTERN_REGISTRY[task_class] = pattern_fn


def derive_domain_from_ledger(ledger: SeedDraftLedger) -> DomainInference:
    """Run every registered pattern against *ledger* and classify.

    Outcomes:

    - **Single match** — exactly one pattern fired. Returns
      ``DomainInference`` with ``classes = {that_class}`` and
      ``reason = "single pattern match"``.
    - **Ambiguous** — two or more patterns fired. Returns
      ``DomainInference`` with the fired classes and
      ``reason = "multiple patterns matched"``. The interview driver
      should ask a disambiguation question (L1-c).
    - **Unmatched** — no pattern fired. Returns ``DomainInference`` with
      empty ``classes``, ``fallback = LIBRARY`` (the safest completion
      gate), and ``reason = "unmatched"``. Callers should also emit a
      ``domain_unmatched`` telemetry event.
    """
    fired: list[TaskClass] = []
    signals: list[str] = []
    for task_class, pattern_fn in _PATTERN_REGISTRY.items():
        if pattern_fn(ledger):
            fired.append(task_class)
            signals.append(task_class.value)
    if not fired:
        return DomainInference(
            classes=frozenset(),
            reason="unmatched",
            fallback=TaskClass.LIBRARY,
            matched_signals=(),
        )
    if len(fired) == 1:
        return DomainInference(
            classes=frozenset(fired),
            reason="single pattern match",
            fallback=None,
            matched_signals=tuple(signals),
        )
    return DomainInference(
        classes=frozenset(fired),
        reason="multiple patterns matched",
        fallback=None,
        matched_signals=tuple(signals),
    )
