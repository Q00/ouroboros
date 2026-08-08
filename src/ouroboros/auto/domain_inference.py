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
    r"without|excluding|sans|"
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


def _strip_negated_signals(text: str, signal_fragment: str) -> str:
    """Remove recognized denials of *signal_fragment* from *text*, keeping
    affirmative-flip expansions ("not just a browser page") intact."""
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


# Browser-context negation mirrors the CLI machinery above — the same
# cue/path/affirmative-flip pipeline applied to web-app vocabulary (#1813
# R1). Only goal prose carries natural-language denials; ledger
# outputs/runtime_context entries are standardized evidence and skip this.
# "single page" alone is document vocabulary ("a single page PDF report");
# only the full single-page-application phrase denotes browser context.
_WEB_APP_GOAL_SIGNAL_FRAGMENT = (
    r"(?:browsers?|web[\s\-]?app(?:lication)?s?|websites?|web\s+uis?|frontends?|"
    r"front[\s\-]ends?|single[\s\-]page\s+app(?:lication)?s?)"
)
_WEB_APP_GOAL_SIGNAL_RE = re.compile(rf"\b{_WEB_APP_GOAL_SIGNAL_FRAGMENT}\b")


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


def _goal_text(ledger: SeedDraftLedger) -> str:
    return _section_text(ledger, "goal")


def _matches_cli(ledger: SeedDraftLedger) -> bool:
    outputs = _section_text(ledger, "outputs")
    runtime = _section_text(ledger, "runtime_context")
    if not (outputs or runtime):
        # Gate: require *some* ledger-side evidence beyond the goal text
        # so single-word "cli" mentions in goal cannot classify alone.
        return False
    output_signal = _any_of(
        outputs,
        ("stdout", "exit code", "printed", "console output", "command output"),
    )
    runtime_signal = _any_of(runtime, ("shell", "terminal", "subprocess", "command line"))
    # Goal-side CLI signal: token-bounded "cli" OR explicit "command line"
    # / "command-line" multi-word phrase, with explicit-negation
    # stripping. Substring-only matching against "cli" would false-
    # positive on "client", "click", "clinic", etc. (first-round PR
    # #1264 blocker), and naked token matching still classifies "not a
    # CLI" / "no CLI" goals as CLI (second-round PR #1264 blocker), so
    # both classes route through `_goal_has_unnegated_cli_signal`.
    # Consumed tooling ("using a command-line image converter") is a
    # dependency, not the produced artifact (#1813 R28).
    goal_text = _strip_consumed_dependencies(_goal_text(ledger))
    # Attributive CLI mentions follow the final-head rule shared with the
    # library and web-app vocabulary (#1813 R61): in "a CLI documentation
    # website" the produced artifact is the website, so the goal-side CLI
    # token carries no ownership when the first NP heads a UI product.
    goal_signal = _goal_has_unnegated_cli_signal(goal_text) and not _goal_first_np_is_ui_headed(
        _goal_text(ledger)
    )
    # Each of the three signals is independently sufficient once the
    # ledger-evidence gate above is satisfied. The earlier form
    # `runtime_signal or (output_signal and (goal_signal or outputs))`
    # made `goal_signal` dead code (outputs is already non-empty past
    # the gate), which blocked cli-todo on ledger_only closures whose
    # conservative-default outputs lack stdout/exit-code vocabulary.
    # See #1170 R2 evidence and #1157 closure policy.
    return runtime_signal or goal_signal or output_signal


def _matches_webhook(ledger: SeedDraftLedger) -> bool:
    inputs = _section_text(ledger, "inputs")
    outputs = _section_text(ledger, "outputs")
    goal = _goal_text(ledger)
    if not (inputs or outputs or goal):
        return False
    has_webhook_in = _any_of(
        inputs + " " + goal,
        ("webhook", "http post", "incoming event", "event payload", "callback url"),
    )
    has_side_effect = _any_of(
        outputs,
        ("side effect", "db row", "database row", "log entry", "stored", "external call"),
    )
    return has_webhook_in and has_side_effect


_PRODUCED_SERVICE_RE = re.compile(
    r"\b(?:serv\w+|expos\w+|provid\w+|offer\w+|host\w+|publish\w+|"
    r"implement\w+|deliver\w+)\b[^,.;]*\b"
    r"(?:rest\s+apis?|apis?|endpoints?|web\s+services?|https?\s+servers?)"
)
_PRODUCED_SERVICE_RESPONSE_RE = re.compile(
    r"\b(?:servers?|backends?|services?|apis?|endpoints?)\b[^,.;]*?\b"
    r"(?:returns?|responds?(?:\s+with)?|sends?|emits?|serves?|produces?)\b"
    r"[^,.;]*?\b(?:https?\s+responses?|json\s+(?:bodies?|responses?))\b"
)
_WEB_SERVICE_SIGNAL_FRAGMENT = (
    r"(?:rest\s+apis?|rest\s+endpoints?|web\s+services?|web\s+servers?|"
    r"https?\s+servers?|apis?|endpoints?)"
)


def _matches_web_service(ledger: SeedDraftLedger) -> bool:
    outputs = _section_text(ledger, "outputs")
    goal = _goal_text(ledger)
    if not (outputs or goal):
        return False
    # Denied service vocabulary is not evidence (#1813 R32): the goal
    # routes through the shared negation strip before keyword matching.
    service_goal = _strip_negated_signals(goal, _WEB_SERVICE_SIGNAL_FRAGMENT)
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
    visible = _strip_consumed_dependencies(
        outputs + ". " + ("" if ui_headed_goal else service_goal)
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


# "game loop", "playable", and "2d game" are unconditionally game-shaped;
# canvas/scene/frame join render/screen as shared rendering vocabulary
# (#1813 R18) — browser drawing surfaces and scene editors are UIs.
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
    # Token-bounded (#1813 R13): substring matching made "iframe" satisfy
    # "frame" and would accept similar embeddings for the other terms.
    if _GAME_CORE_RE.search(visible):
        return True
    if not _GAME_SHARED_SHAPE_RE.search(visible):
        return False
    if _GAME_DOMAIN_RE.search(visible):
        return True
    return not _ledger_has_browser_context(ledger)


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


_WEB_APP_ARTIFACT_PHRASE_RE = re.compile(
    r"\b(?:web[\s\-]?app(?:lication)?s?|webapps?|websites?|web\s+uis?|frontends?|"
    r"front[\s\-]ends?|single[\s\-]page\s+app(?:lication)?s?)\b"
)
_UI_PRODUCT_HEAD_FRAGMENT = (
    r"(?:apps?|applications?|webapps?|uis?|interfaces?|pages?|"
    r"frontends?|sites?|websites?|dashboards?|consoles?|portals?|"
    r"players?|editors?|viewers?|clients?|panels?|forms?)"
)
# Shared first-noun-phrase grammar (#1813 R50/R52/R55): routine intent
# wrappers, an optional specificational copula ("the product is ..."),
# at most two lead tokens, ONE determiner, then modifiers. A determiner
# deeper in the chain opens an embedded noun phrase, participles are
# verbal outside the nominal-gerund slot, and structural prepositions or
# relativizers end the phrase.
_NP_CHAIN_STOP_FRAGMENT = (
    r"(?:that|which|who|to|for|of|from|with|without|via|through|by|on|in|at|"
    r"and|or|nor|but|about|regarding|concerning)"
)
_QUALIFIER_WRAPPER_FRAGMENT = (
    r"(?:(?:i|we|you|please|kindly|really|just|ideally|ultimately|"
    r"eventually|currently|first|next|now|then|also|help|me|us|let[’']?s|"
    r"want|wants|wanted|need|needs|needed|would|like|love|hope|hoping|"
    r"plan|planning|aim|aiming|intend|intending|going|trying|try|wish|"
    r"wishes|to)\s+){0,6}?"
)
# A specificational copula declares the produced artifact directly
# (#1813 R55/R58): a definite, possessive, or demonstrative subject plus
# a (possibly modal) copula, optionally followed by an intent infinitive
# — "the product is ...", "our deliverable should be ...", "this should
# be ...", "the goal is to build ...".
_COPULAR_VERB_FRAGMENT = (
    r"(?:is|are|was|were|will\s+be|would\s+be|should\s+be|must\s+be|"
    r"shall\s+be|becomes?|ought\s+to\s+be|needs?\s+to\s+be|has\s+to\s+be)"
)
_COPULAR_FRAME_FRAGMENT = (
    rf"(?:(?:(?:the|our|my|your)\s+(?:[\w\-]+\s+){{1,2}}?|(?:this|that|it)\s+)"
    rf"{_COPULAR_VERB_FRAGMENT}\s+(?:to\s+)?)?"
)
_NP_LEAD_FRAGMENT = rf"(?:(?!{_NP_CHAIN_STOP_FRAGMENT}\b)(?![\w'’\-]+ing\b)[\w'’\-]+\s+){{0,2}}?"
_NP_DETERMINER_FRAGMENT = r"(?:(?:an?|the|one|this|our|my|your)\s+)?"
_NP_MODIFIER_TOKEN = (
    rf"(?!(?:an?|the|one|this|{_NP_CHAIN_STOP_FRAGMENT})\b)"
    r"(?![\w'’\-]+ing\b)[\w'’\-]+"
)
_NOMINAL_GERUND_FRAGMENT = (
    r"(?:billing|landing|onboarding|shopping|reporting|logging|"
    r"messaging|streaming|banking|invoicing|pricing|staging|voting|"
    r"polling)"
)
_FIRST_NP_PREFIX = (
    r"^"
    + _QUALIFIER_WRAPPER_FRAGMENT
    + _COPULAR_FRAME_FRAGMENT
    + _NP_LEAD_FRAGMENT
    + _NP_DETERMINER_FRAGMENT
)
# Browser-qualified UI product heads own the artifact the way the literal
# web-app vocabulary does (#1813 R44): in "browser-based admin portal" the
# browser qualifier is the web signal and the UI noun is the produced
# head. The qualifier and head must live in the goal's own first noun
# phrase (#1813 R55): a goal-final prepositional object ("a report on
# browser pages") is a subject, not the produced artifact, so the same
# first-NP prefix guards this rule as the postnominal one.
_BROWSER_QUALIFIED_UI_HEAD_RE = re.compile(
    _FIRST_NP_PREFIX
    + rf"(?:{_NP_MODIFIER_TOKEN}\s+){{0,4}}?"
    # Bare "web" is a qualifier only in this bounded form — directly
    # modifying a UI product head ("web dashboard", #1813 R60); it stays
    # out of the general signal vocabulary.
    + rf"(?:{_WEB_APP_GOAL_SIGNAL_FRAGMENT}|web[\s\-]based|in[\s\-]browser|web)"
    + rf"(?:[\s\-]+{_NP_MODIFIER_TOKEN}){{0,3}}?"
    + rf"(?:[\s\-]+{_NOMINAL_GERUND_FRAGMENT})?"
    + rf"[\s\-]+{_UI_PRODUCT_HEAD_FRAGMENT}[\s.!?]*$"
)
# Ownership is word-order independent (#1813 R45): a postnominal
# runtime/availability qualifier ("an admin portal for the browser",
# "... that runs in the browser", "... accessible from a browser") owns
# the artifact when the goal's OWN head is a UI product noun and the
# browser reference is the execution environment (bare browser tokens —
# web-app targets stay consumer relationships, R44). The head must sit in
# the goal's first noun phrase: a relativizer or preposition before it
# means the UI noun belongs to another artifact's clause ("a CLI that
# opens a page in the browser"), and an artifact noun between head and
# qualifier re-heads the phrase ("an admin portal generator for the
# browser").
# The span between a UI head and its environment preposition is
# predicative — relativizers, copulas, adverbs, and runtime/availability
# predicates form a closed grammar (#1813 R47). Any other token after the
# head is nominal and re-heads the phrase ("admin portal accessibility
# audit", "admin portal documentation", "admin portal test harness"), so
# unlisted words block by default instead of an artifact-noun denylist
# blocking by enumeration.
# One closed runtime-verb vocabulary shared by every grammar position —
# direct predicate ("must function in browsers"), participle, and the
# to-infinitive complement — so the branches cannot drift apart (#1813
# R54).
_RUNTIME_VERB_FRAGMENT = (
    r"(?:runs?|running|run|operates?|operating|operate|works?|working|"
    r"work|functions?|functioning|function|loads?|loading|load|opens?|"
    r"opening|open|renders?|rendered|rendering|render|serves?|served|"
    r"serving|serve|executes?|executing|execute|displays?|displayed|"
    r"display|lives?|living|live|behaves?|behave)"
)
_POSTNOMINAL_PREDICATE_TOKEN = (
    r"(?:[\w\-]+ly|also|only|still|already|now|and|or|"
    r"is|are|was|were|be|being|been|will|would|can|could|may|might|must|"
    r"should|shall|has|have|had|ought|expected|required|requires?|needs?|"
    r"needed|supposed|planned|guaranteed|mandated|obliged|obligated|"
    r"compelled|forced|slated|scheduled|destined|"
    rf"{_RUNTIME_VERB_FRAGMENT}|loads?|loading|"
    r"delivers?|delivered|displayed|shown|hosted|"
    r"usable|useable|used|accessible|available|"
    r"compatible|supported|"
    r"reachable|viewable|accessed|opened|intended|designed|meant|built|"
    r"made|optimized|optimised|tailored|deployed|published|distributed|"
    r"offered|provided)"
)
_POSTNOMINAL_BROWSER_QUALIFIER_RE = re.compile(
    # The produced head is the goal's FIRST noun phrase (#1813 R50): at
    # most two lead tokens (imperative verbs), neither a participle, then
    # ONE optional determiner, then modifiers. A determiner deeper in the
    # chain opens an embedded noun phrase ("documentation introducing AN
    # admin portal"), so a UI noun there is a relationship target no
    # matter which verb introduced it — no verb enumeration involved.
    # Participles are barred from every chain slot except attributively
    # before the head, where nominal gerunds are ordinary product
    # vocabulary ("billing dashboard", "landing page") but relational and
    # subject-matter stems keep their verbal reading.
    _FIRST_NP_PREFIX
    + rf"(?:{_NP_MODIFIER_TOKEN}\s+){{0,6}}?"
    # The attributive slot admits only nominal gerunds — established
    # compound-noun product vocabulary (#1813 R51). An unknown participle
    # is verbal by default ("a report evaluating dashboards"), so
    # subject and relationship verbs block without enumeration.
    + rf"(?:{_NOMINAL_GERUND_FRAGMENT}\s+)?"
    + _UI_PRODUCT_HEAD_FRAGMENT
    # A comma may introduce a dependent qualifier ("an admin portal,
    # accessible in the browser") — a coordinator after it is real
    # coordination and stays outside the phrase (#1813 R47).
    + r"(?:\s*,\s+(?!(?:and|or|nor|but|plus|alongside|not|no)\b)|\s+)"
    r"(?:(?:that|which)\s+)?"
    rf"(?:{_POSTNOMINAL_PREDICATE_TOKEN}\s+){{0,4}}?"
    # Routine infinitival and "for use in" qualifiers declare the same
    # environment (#1813 R48): "designed to run in browsers", "for use in
    # browsers". The infinitive complement comes from the same closed
    # runtime/availability sets — active, passive ("to be used"), and
    # adjectival ("to be accessible") alike (#1813 R49) — so "to
    # test/audit" action targets stay outside the grammar.
    rf"(?:to\s+(?:be\s+)?(?:{_RUNTIME_VERB_FRAGMENT}|"
    r"used|accessed|opened|served|rendered|"
    r"displayed|delivered|operated|executed|loaded|viewed|reached|hosted|"
    r"accessible|available|usable|useable|reachable|viewable)\s+)?"
    r"(?:for\s+use\s+)?"
    # Compatibility/support declarations with a browser target are the
    # same environment ownership (#1813 R53): "compatible with modern
    # browsers", "available across browsers", "supporting browsers". The
    # target below stays bare browser tokens, so web-app targets remain
    # consumer relationships.
    r"(?:in|inside|within|for|from|through|via|on|with|across|"
    r"supports?|supporting)\s+"
    # Ordinary spelling variants are equivalent environments (#1813 R46):
    # "a web browser", "modern browsers".
    r"(?:(?:an?|the|any|all|every|most)\s+)?"
    r"(?:(?:web|modern|current|standard|major|mobile|desktop|mainstream|"
    r"common|popular|supported|default|local|system)\s+){0,2}?"
    r"browsers?\b"
    # The environment phrase must END at the browser token (#1813 R46): a
    # trailing noun re-heads it into an artifact ("the browser extension",
    # "the browser automation suite"). Function words and the R45
    # "browser use" idiom keep the environment reading.
    r"(?!\s+(?!(?:and|or|nor|but|use|usage|without|with|for|on|in|at|by|"
    r"from|via|through|to|so|that|which|because|since|while|when|where|"
    r"using|during|after|before|only|instead|rather|too|as|alongside)\b)[\w'’\-]+)"
)


# "to <verb> X" is an action target; "to a/an/the X" is a plain PP and
# "in addition to an X" a coordinator (#1813 R34) — articles are excluded.
_INFINITIVE_TARGET_RE = re.compile(r"\bto\s+(?!be\b|an?\b|the\b)[\w\-]+\s+[^,.;]*")
_RELATIONAL_TARGET_RE = re.compile(
    r"\b(?:targeting|supporting|serving|powering|backing|aimed\s+at|"
    r"used\s+by|consumed\s+by|embedded\s+(?:in|into|within|inside)|"
    r"nested\s+(?:in|within|inside)|contained\s+(?:in|within|inside)|"
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
    r"front[\s\-]ends?|single[\s\-]page\s+app(?:lication)?s?|browsers?)\b"
    r"(?:\s*(?:,|\band\b|\bor\b|\bas\s+well\s+as\b|\balong\s+with\b|\bplus\b|/)\s*(?:(?:an?|the|my|our|your)\s+)?[\w\-'’]+(?:\s+[\w\-'’]+){0,2}){0,3}"
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
    # "to test/analyze/deploy a web app" names the target of another
    # artifact, not the produced one (#1813 R28), and participial
    # relations ("SDK targeting web apps", "package used by web apps")
    # name the consumer (#1813 R29).
    core = _INFINITIVE_TARGET_RE.sub(" ", core)
    core = _strip_consumer_relations(core)
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
    # (#1813 W1).
    goal_text = _INFINITIVE_TARGET_RE.sub(" ", goal_text)
    pieces = _GOAL_CONJUNCT_SPLIT_RE.split(goal_text)
    if len(pieces) < 2:
        return False
    # Each piece is judged on its own head (#1813 R32): a web mention
    # that is the target of documentation/plugins/adapters ("with
    # documentation for web apps") is not a co-produced app.
    piece_cores = [_CONTENT_CLAUSE_RE.sub(" ", piece) for piece in pieces]

    def _piece_declares_web_product(core: str) -> bool:
        matches = list(_WEB_APP_ARTIFACT_PHRASE_RE.finditer(core))
        if not matches or _LIBRARY_INTENT_RE.search(core):
            return False
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


_POSTPOSITIVE_BROWSER_DENIAL_RE = re.compile(
    rf"\b{_WEB_APP_GOAL_SIGNAL_FRAGMENT}(?:\s+[\w\-]+){{0,2}}?\s+"
    r"(?:is\s+|are\s+|was\s+|were\s+|has\s+been\s+|have\s+been\s+|"
    r"had\s+been\s+|will\s+be\s+)?"
    r"(?:\w+ly\s+)?"
    r"(?:(?:isn[’']?t|aren[’']?t|wasn[’']?t|weren[’']?t|"
    r"won[’']?t\s+be|wouldn[’']?t\s+be|shouldn[’']?t\s+be|"
    r"can[’']?t\s+be|cannot\s+be|mustn[’']?t\s+be)\s+(?:\w+ly\s+)?"
    r"(?:supported|available|allowed|permitted|enabled|possible|"
    r"offered|provided|present|included)|"
    r"not\s+(?:be\s+)?(?:supported|available|allowed|permitted|enabled|possible|"
    r"offered|provided|present|included)|"
    r"unsupported|disabled|unavailable|dropped|excluded|"
    r"turned\s+off|removed|prohibited|forbidden|banned|blocked|stripped|"
    r"denied|denylisted|off[\s\-]limits)\b"
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


# "browser-like", "web-app-style": a similarity suffix marks the web token
# as a comparison reference, not the produced artifact (#1813 R44).
_WEB_SIMILARITY_MODIFIER_RE = re.compile(
    rf"\b{_WEB_APP_GOAL_SIGNAL_FRAGMENT}[\s\-]+(?:like|style|styled|esque|inspired|themed)\b"
)
# A browser/web token followed by a component-artifact noun is re-headed
# by that component (#1813 R60): a "browser extension settings page" is
# an extension surface, not a standalone browser application, so the
# browser word carries no web-product evidence. The component noun stays
# behind for the other matchers.
_BROWSER_COMPONENT_RE = re.compile(
    r"\b(?:web|browsers?)[\s\-]+"
    r"(?=(?:extensions?|plugins?|add[\s\-]?ons?|automation|devtools?|drivers?)\b)"
)


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
    runtime = _section_text(ledger, "runtime_context")
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
    return bool(_SECTION_BROWSER_ENV_RE.search(_section_browser_context_text(ledger))) or (
        _goal_has_unnegated_web_app_signal(goal_for_context)
    )


# A browser token in the standardized sections counts as the execution
# environment only when nothing re-heads it (#1813 R57/R58): "Browser"
# and "Runs in browsers" qualify; "Browser extension runtime" names
# another artifact, and an EMBEDDED browser ("Embedded browser runtime
# in Electron") is a component of another host, not the environment.
# Environment/function words after the token keep the reading.
_SECTION_BROWSER_ENV_RE = re.compile(
    r"(?<!embedded )(?<!headless )(?<!in-app )(?<!inline )(?<!internal )"
    r"(?<!integrated )(?<!bundled )"
    rf"\b{_WEB_APP_GOAL_SIGNAL_FRAGMENT}\b"
    r"(?!\s+(?!(?:and|or|nor|but|use|usage|without|with|for|on|in|at|by|"
    r"from|via|through|to|so|that|which|because|since|while|when|where|"
    r"using|during|after|before|only|instead|rather|too|as|alongside|"
    r"runtime|context|execution|environment|sessions?|windows?|tabs?)\b)[\w'’\-]+)"
)


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
    goal = _LIBRARY_SUBJECT_CLAUSE_RE.sub(" ", _goal_text(ledger))
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
_GOAL_COMPONENT_TARGET_RE = re.compile(
    r"\b(?:for|of|in|inside|within|into)\s+(?:an?\s+|the\s+)?(?:[\w\-]+\s+){0,2}?"
    r"(?:extensions?|plugins?|add[\s\-]?ons?|addons?|sidebars?|popups?|"
    r"toolbars?|overlays?|devtools?)\b"
    r"(?!\s+(?!(?:and|or|nor|but|without|with|for|on|in|at|by|from|via|"
    r"through|to|so|that|which)\b)[\w'’\-]+)"
)


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


_NP_CHAIN_STOP_WORDS = frozenset(
    [
        # Predicative and adversative continuations end the phrase too
        # (#1813 R57): in "a web app compatible with ..." or "a web app
        # rather than ..." the head is already behind us.
        "rather",
        "than",
        "instead",
        "versus",
        "vs",
        "not",
        "no",
        "is",
        "are",
        "was",
        "were",
        "compatible",
        "available",
        "accessible",
        "usable",
        "useable",
        "supported",
        "reachable",
        "viewable",
        "intended",
        "designed",
        "meant",
        "built",
        "made",
        "optimized",
        "optimised",
        "tailored",
        "deployed",
        "published",
        "distributed",
        "offered",
        "provided",
        "delivered",
        "hosted",
        "shown",
        "displayed",
        "served",
        "used",
        "expected",
        "required",
        "needed",
        "supposed",
        "planned",
        "guaranteed",
        "mandated",
        "that",
        "which",
        "who",
        "to",
        "for",
        "of",
        "from",
        "with",
        "without",
        "via",
        "through",
        "by",
        "on",
        "in",
        "at",
        "and",
        "or",
        "nor",
        "but",
        "about",
        "regarding",
        "concerning",
    ]
)
_NP_DETERMINER_WORDS = frozenset(["a", "an", "the", "one", "this", "our", "my", "your"])
_NOMINAL_GERUND_WORDS = frozenset(
    [
        "billing",
        "landing",
        "onboarding",
        "shopping",
        "reporting",
        "logging",
        "messaging",
        "streaming",
        "banking",
        "invoicing",
        "pricing",
        "staging",
        "voting",
        "polling",
    ]
)
# A component noun anywhere in the first NP owns the artifact (#1813
# R61): "browser extension settings page" is an extension surface, and
# no runtime wording — specialized or generic — can hand it back to
# web_app.
_COMPONENT_NOUN_WORDS = frozenset(
    [
        "extension",
        "extensions",
        "plugin",
        "plugins",
        "addon",
        "addons",
        "add-on",
        "add-ons",
        "devtool",
        "devtools",
        "driver",
        "drivers",
        "automation",
        # Native browser component surfaces (#1813 R64): a qualified
        # sidebar/popup is a component like a qualified extension.
        "sidebar",
        "sidebars",
        "popup",
        "popups",
        "pop-up",
        "pop-ups",
        "toolbar",
        "toolbars",
        "overlay",
        "overlays",
        "menu",
        "menus",
    ]
)
# A lone leading token is a product NP only when it is a noun (#1813
# R65): "Spreadsheet in the browser" declares a product, "Migrate from
# ..." is a bare imperative. The action verbs form the closed set.
_BARE_ACTION_VERB_WORDS = frozenset(
    [
        "build",
        "create",
        "make",
        "develop",
        "implement",
        "design",
        "write",
        "produce",
        "deliver",
        "ship",
        "craft",
        "construct",
        "add",
        "expose",
        "provide",
        "offer",
        "publish",
        "host",
        "migrate",
        "convert",
        "port",
        "adapt",
        "promote",
        "move",
        "upgrade",
        "switch",
        "transition",
        "modernize",
        "rewrite",
        "transform",
        "refactor",
        "consolidate",
        "turn",
        "submit",
        "send",
        "upload",
        "forward",
        "deploy",
        "fix",
        "update",
        "improve",
        "optimize",
        "test",
        "audit",
        "scan",
        "check",
        "analyze",
        "review",
        "inspect",
        "monitor",
        "run",
        "generate",
        "launch",
        "start",
    ]
)
# The gate WALK accepts more nominal gerunds than the ownership REs
# (#1813 R56): "browser monitoring dashboard" is Q00's own product
# example, and the walk only filters — it cannot grant — so activity
# gerunds are safe here while they stay excluded from the granting
# attributive slot (R51).
_NP_WALK_GERUND_WORDS = _NOMINAL_GERUND_WORDS | {
    "monitoring",
    "testing",
    "drawing",
    "editing",
    "tracking",
    "auditing",
    "scheduling",
    "booking",
}
_UI_HEAD_NOUN_RE = re.compile(rf"^{_UI_PRODUCT_HEAD_FRAGMENT}$")
# The gate's head decision is default-allow with a NON-product denylist
# (#1813 R63): legitimate UI product nouns need no pre-enumeration
# ("kanban board", "survey builder", "spreadsheet", "workspace"), while
# report/tool/component-class heads keep their own artifact identity.
_NON_PRODUCT_HEAD_WORDS = frozenset(
    [
        "report",
        "reports",
        "documentation",
        "doc",
        "docs",
        "guide",
        "guides",
        "manual",
        "manuals",
        "summary",
        "summaries",
        "audit",
        "audits",
        "benchmark",
        "benchmarks",
        "harness",
        "harnesses",
        "cli",
        "clis",
        "tool",
        "tools",
        "toolkit",
        "toolkits",
        "utility",
        "utilities",
        "generator",
        "generators",
        "scaffolder",
        "scaffolders",
        "compiler",
        "compilers",
        "converter",
        "converters",
        "crawler",
        "crawlers",
        "scraper",
        "scrapers",
        "scanner",
        "scanners",
        "bot",
        "bots",
        "script",
        "scripts",
        "suite",
        "suites",
        "pipeline",
        "pipelines",
        "library",
        "libraries",
        "sdk",
        "sdks",
        "package",
        "packages",
        "api",
        "apis",
        "endpoint",
        "endpoints",
        "service",
        "services",
        "server",
        "servers",
        "backend",
        "backends",
        "daemon",
        "daemons",
        "analysis",
        "analyses",
        "analyzer",
        "analyzers",
        "exporter",
        "exporters",
        "importer",
        "importers",
        "wrapper",
        "wrappers",
        "adapter",
        "adapters",
    ]
)


def _goal_first_np_head(goal_text: str) -> str | None:
    """Walk the goal's first noun phrase and return its final head.

    The walk mirrors the first-NP grammar: structural prepositions and
    relativizers end the phrase, a second determiner opens an embedded
    noun phrase, and participles are verbal outside phrase-initial
    position and the nominal-gerund vocabulary. Returns ``None`` when no
    product NP exists — a bare-verb walk ("Migrate from ..."), or a
    qualified component compound ("browser extension settings page",
    "Chrome plugin popup page"), whose surfaces belong to the component
    (#1813 R61-R63)."""
    np_tokens: list[str] = []
    seen_determiner = False
    content_since_determiner = 0
    first_segment = re.split(r"[.;:!?,]", goal_text)[0]
    for token in re.findall(r"[\w'’\-]+", first_segment):
        lowered = token.lower()
        # Component ownership is positional (#1813 R62/R63): a QUALIFIED
        # component — one with a preceding content token, whether
        # "browser extension" or the brand-qualified "Chrome extension" —
        # is the produced artifact and its surfaces belong to it, while a
        # phrase-initial component is the subject of what follows
        # ("plugin management dashboard").
        if lowered in _COMPONENT_NOUN_WORDS and content_since_determiner > 0:
            return None
        if lowered in _NP_CHAIN_STOP_WORDS:
            break
        if lowered in _NP_DETERMINER_WORDS:
            if seen_determiner:
                break
            seen_determiner = True
            content_since_determiner = 0
            continue
        # An unknown participle is attributive in phrase-initial position
        # ("a recruiting dashboard") and verbal once content precedes it
        # ("a report evaluating dashboards") — position decides, not a
        # vocabulary list (#1813 R62); the known nominal gerunds stay
        # recognized in any slot.
        if (
            lowered.endswith("ing")
            and lowered not in _NP_WALK_GERUND_WORDS
            and content_since_determiner > 0
        ):
            break
        np_tokens.append(lowered)
        content_since_determiner += 1
    # A bare-verb walk ("Migrate from ...") never reached a product NP;
    # articleless imperatives ("Create browser-based kanban board") and
    # one-word product declarations ("Spreadsheet in the browser") did
    # (#1813 R63-R65) — only a lone ACTION VERB fails to mark a phrase.
    if not np_tokens:
        return None
    if not seen_determiner and len(np_tokens) < 2 and np_tokens[0] in _BARE_ACTION_VERB_WORDS:
        return None
    # A component noun in HEAD position is component-owned regardless of
    # qualifier order (#1813 R65): "a sidebar for Firefox" is the same
    # surface as "a Firefox sidebar".
    if np_tokens[-1] in _COMPONENT_NOUN_WORDS:
        return None
    return np_tokens[-1]


def _goal_first_np_has_ui_shape(goal_text: str) -> bool:
    """Output composition corroborates ownership; it cannot create it
    (#1813 R55). Default-allow with a non-product denylist (#1813 R63):
    legitimate UI product nouns need no pre-enumeration ("kanban board",
    "spreadsheet"), while report/tool/component-class heads keep their
    own artifact identity."""
    head = _goal_first_np_head(goal_text)
    return head is not None and head not in _NON_PRODUCT_HEAD_WORDS


def _goal_first_np_is_ui_headed(goal_text: str) -> bool:
    """Affirmative UI-product head for cross-class suppression (#1813
    R61/R63): suppressing cli/service goal evidence demands the stricter
    claim that the phrase is HEADED by a UI product noun ("a CLI
    documentation website"), not merely that its head is unlisted ("a
    command line habit tracker")."""
    head = _goal_first_np_head(goal_text)
    return head is not None and bool(_UI_HEAD_NOUN_RE.match(head))


_MANIPULATED_TARGET_RE = re.compile(
    r"\b(?:opens?|opening|submits?|submitting|clicks?|clicking|fills?|filling|"
    r"screenshots?|captures?|capturing|crawls?|crawling|scrapes?|scraping|"
    r"visits?|visiting|navigates?|navigating|audits?|auditing|inspects?|"
    r"inspecting|aggregat\w*|pars\w*|extracts?|extracting|scans?|scanning|"
    r"verif\w*|checks?|checking|asserts?|asserting)\s+"
    r"(?:an?\s+|the\s+)?(?:[\w\-'’]+\s+){0,2}?"
    r"(?:forms?|pages?|panels?|buttons?|screens?|dialogs?|modals?|menus?)\b"
)


_UI_SIGNAL_STRIP_FRAGMENT = (
    r"(?:user\s+interface|forms?|panels?|buttons?|dashboards?|pages?|"
    r"screens?|dialogs?|modals?|menus?|toolbars?|sidebars?)"
)


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
    # An affirmative web-product request owns its output description
    # (#1813 R27): when the goal's artifact head is a web app, the
    # standardized outputs describe that requested product, and no widget
    # catalog is required. Non-web-product goals (extensions, crawlers,
    # test suites, CLIs) still need genuine UI-composition evidence.
    if _goal_artifact_head_is_web_app(_goal_text(ledger)):
        return True
    # An explicitly co-produced web app ("a Python library with an admin
    # web app") retains web ownership regardless of incidental output
    # wording (#1813 R30) — the overlap stays an honest ambiguity.
    if _goal_has_web_co_product_conjunct(_goal_text(ledger)):
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
    guard_goal = _strip_consumed_dependencies(_SUBJECT_CLAUSE_RE.sub(" ", _goal_text(ledger)))
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
    # A UI surface declared FOR a component belongs to that component
    # (#1813 R66), whatever the runtime wording — the postfix mirror of
    # the first-NP component rule, guarding the composition and semantic
    # fallbacks after the affirmative grants have had their say.
    if _GOAL_COMPONENT_TARGET_RE.search(re.split(r"[.;:!?]", _goal_text(ledger))[0]):
        return False
    # The product-head exception accepts standardized runtime evidence
    # (#1813 R61): "webhook monitoring dashboard" with a browser
    # execution environment is a dashboard whose subject is monitoring,
    # not a monitoring tool.
    if _INSPECTION_TOOL_GOAL_RE.search(_goal_text(ledger)) and not (
        _goal_has_browser_ui_product_head(_goal_text(ledger))
        or (
            _goal_first_np_has_ui_shape(_goal_text(ledger))
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
    # extension runtime") does not qualify.
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


def _strip_consumed_dependencies(text: str) -> str:
    """Remove relational and prenominal dependency/client declarations."""

    def _spare_destinations(match: re.Match[str]) -> str:
        if re.match(r"to\s", match.group(0)) is None:
            return " "
        segment = re.split(r"[.;:!?]", text[: match.start()])[-1]
        if _DESTINATION_CONTEXT_RE.match(segment):
            return match.group(0)
        return " "

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
