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
    r"instead\s+of|rather\s+than)"
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
        r"(?P<path>(?:\s+\S+){0,7}?)"
        rf"\s+{denied}"
        rf"(?:(?:\s*,\s*(?:or\s+|and\s+|nor\s+)?|\s+(?:or|and|nor)\s+|\s*/\s*)"
        rf"(?:an?\s+|the\s+)?{denied})*\b"
    )
    prefix = re.compile(rf"\bnon[\s\-]?{signal_fragment}\b")
    return negated, prefix


def _strip_negated_signals(text: str, signal_fragment: str) -> str:
    """Remove recognized denials of *signal_fragment* from *text*, keeping
    affirmative-flip expansions ("not just a browser page") intact."""
    negated, prefix = _negation_res_for(signal_fragment)

    def _keep_if_affirmative(match: re.Match[str]) -> str:
        path = match.group("path") or ""
        return match.group(0) if _AFFIRMATIVE_FLIP_RE.search(path) else " "

    return prefix.sub(" ", negated.sub(_keep_if_affirmative, text))


# Browser-context negation mirrors the CLI machinery above — the same
# cue/path/affirmative-flip pipeline applied to web-app vocabulary (#1813
# R1). Only goal prose carries natural-language denials; ledger
# outputs/runtime_context entries are standardized evidence and skip this.
# "single page" alone is document vocabulary ("a single page PDF report");
# only the full single-page-application phrase denotes browser context.
_WEB_APP_GOAL_SIGNAL_FRAGMENT = (
    r"(?:browser|web[\s\-]?app(?:lication)?|frontend|front[\s\-]end|"
    r"single[\s\-]page\s+app(?:lication)?)"
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
    }
)
_DENIED_PP_TAIL_RE = re.compile(r"\b(?:for|about)\b.*$")
_DENIED_PIECE_SPLIT_RE = re.compile(r"\s*(?:,|/|\bor\b|\band\b|\bnor\b)\s*")


_SUBJECT_CLAUSE_RE = re.compile(r"\b(?:for|with|about)\s+[^,.;]*")


def _goal_denies_web_app_artifact(goal_text: str) -> bool:
    """True when a denial rejects the app/UI artifact type itself.

    Dominance keys on the head noun of each denied alternative (#1813
    R16): "not a web app" and "non-web-app" deny the artifact, while
    "not a frontend SDK" denies an SDK and "not a browser extension for
    login pages" denies an extension — the modifier "frontend" and the
    PP object "pages" are not what is being denied.
    """
    negated, prefix = _negation_res_for(_WEB_APP_GOAL_SIGNAL_FRAGMENT)
    spans = [
        match.group(0)
        for match in negated.finditer(goal_text)
        if not _AFFIRMATIVE_FLIP_RE.search(match.group("path") or "")
    ]
    spans.extend(match.group(0) for match in prefix.finditer(goal_text))
    for span in spans:
        core = _DENIED_PP_TAIL_RE.sub(" ", span)
        for piece in _DENIED_PIECE_SPLIT_RE.split(core):
            words = re.findall(r"[a-z]+", piece)
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
    goal_text = _goal_text(ledger)
    goal_signal = _goal_has_unnegated_cli_signal(goal_text)
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


def _matches_web_service(ledger: SeedDraftLedger) -> bool:
    outputs = _section_text(ledger, "outputs")
    goal = _goal_text(ledger)
    if not (outputs or goal):
        return False
    api_signal = _any_of(
        outputs + " " + goal,
        (
            "rest endpoint",
            "rest api",
            "http response",
            "json body",
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
_GAME_RENDER_OR_SCREEN_RE = re.compile(r"\brender(?:s|ing|ed)?\b|\bscreens?\b")

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


_GAME_FAST_PATH_RE = re.compile(
    r"\b(?:frames?|canvas(?:es)?|game\s+loops?|playable|2d\s+games?|scenes?)\b"
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
    if _GAME_FAST_PATH_RE.search(visible):
        return True
    if not _GAME_RENDER_OR_SCREEN_RE.search(outputs + " " + goal):
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
    r"documentation|docs|examples?|objects?|fixtures?|locators?|models?))"
)
# The front position is a denylist, not an allowlist (#1813 R9): a finite
# anchor list rejected ordinary product widgets ("password reset screen",
# "shopping cart page", "modal dialog"). Any modifier word is accepted
# except documentation/API-artifact vocabulary, which carries the
# documented library false positives.
_UI_DOC_MODIFIER = r"(?:documentation|docs|manual|wiki|readme|reference|examples?|api|help)"
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
    r"navigation\s+bars?|data\s+tables?"
    r")\b"
)


# Whole manifest/lockfile path tokens ("packages/web/package.json",
# "package-lock.json") and the token-bounded library word (#1813 R5).
_MANIFEST_TOKEN_RE = re.compile(r"\S*package(?:-lock)?\.json\S*")
_LIBRARY_PACKAGE_WORD_RE = re.compile(r"\bpackage\b")


def _ledger_has_browser_context(ledger: SeedDraftLedger) -> bool:
    """Affirmative browser context: standardized outputs/runtime keywords,
    or an unnegated goal-side web-app signal. Shared by the web_app
    matcher and by game_2d's ceding rule for render/screen vocabulary."""
    outputs = _section_text(ledger, "outputs")
    runtime = _section_text(ledger, "runtime_context")
    return _any_of(
        outputs + " " + runtime,
        (
            "browser",
            "web app",
            "webapp",
            "web application",
            "frontend",
            "front-end",
            "single-page app",
            "single page app",
        ),
    ) or _goal_has_unnegated_web_app_signal(_goal_text(ledger))


# Artifact-intent vocabulary — exactly the signals _matches_library treats
# as authoritative ("reusable" alone is not one, #1813 R12), and denials
# are stripped before either matcher consults the goal ("not a library"
# is not intent).
_LIBRARY_INTENT_RE = re.compile(
    r"\b(?:importable|librar(?:y|ies)|sdks?|api\s+surface|public\s+api|package)\b"
)
_LIBRARY_INTENT_FRAGMENT = (
    r"(?:librar(?:y|ies)|sdks?|packages?|api\s+surface|public\s+api|importable)"
)


def _library_visible_goal(ledger: SeedDraftLedger) -> str:
    return _strip_negated_signals(_goal_text(ledger), _LIBRARY_INTENT_FRAGMENT)


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
    # Subject/secondary clauses in the goal ("for game leaderboards",
    # "with a public API") name what the app is about or co-produces, not
    # its artifact shape — they do not surrender ownership (#1813 R16).
    ownership_goal = _SUBJECT_CLAUSE_RE.sub(" ", _goal_text(ledger))
    intent_text = _MANIFEST_TOKEN_RE.sub(
        " ",
        outputs + " " + _strip_negated_signals(ownership_goal, _LIBRARY_INTENT_FRAGMENT),
    )
    if _LIBRARY_INTENT_RE.search(intent_text):
        return False
    game_text = outputs + " " + _strip_negated_signals(ownership_goal, _GAME_GOAL_SIGNAL_FRAGMENT)
    if _GAME_DOMAIN_RE.search(game_text):
        return False
    # Two-signal AND (the webhook shape): browser context plus UI
    # composition. Goal-side browser mentions route through the negation
    # strip so "not a browser page" is not positive evidence. UI
    # composition is required from standardized ledger outputs ONLY (#1813
    # R2): goal prose mentions UI vocabulary in denials ("without forms or
    # pages") and domain references ("browser form parsing"), neither of
    # which asserts that the artifact HAS a UI.
    return _ledger_has_browser_context(ledger) and bool(_UI_COMPOSITION_RE.search(outputs))


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
    text = _MANIFEST_TOKEN_RE.sub(" ", outputs + " " + _library_visible_goal(ledger))
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
