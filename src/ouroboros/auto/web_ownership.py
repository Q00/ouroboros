"""Web-app ownership grammar for L1-b domain inference (#1813).

Extracted from ``domain_inference`` (#1813 R88 module-size split): the
web-app signal vocabulary, the first-noun-phrase grammar, the
prenominal/postnominal browser-qualifier rules, the component-artifact
contract, and the environment/identity regexes. Everything here is
text-level — no ledger types — so ``domain_inference`` imports one way.
"""

from __future__ import annotations

import re

# Browser-context negation mirrors the CLI machinery above — the same
# cue/path/affirmative-flip pipeline applied to web-app vocabulary (#1813
# R1). Only goal prose carries natural-language denials; ledger
# outputs/runtime_context entries are standardized evidence and skip this.
# "single page" alone is document vocabulary ("a single page PDF report");
# only the full single-page-application phrase denotes browser context.
_WEB_APP_GOAL_SIGNAL_FRAGMENT = (
    r"(?:browsers?|web[\s\-]?app(?:lication)?s?|websites?|web\s+uis?|frontends?|"
    r"front[\s\-]ends?|single[\s\-]page\s+app(?:lication)?s?|spas?|pwas?)"
)
_WEB_APP_GOAL_SIGNAL_RE = re.compile(rf"\b{_WEB_APP_GOAL_SIGNAL_FRAGMENT}\b")

# Concrete browser names are ordinary browser evidence (#1813 R104): a
# runtime of "Chrome and Firefox" declares the same environment as
# "Modern browsers". The set is closed to unambiguous names — bare
# "edge" and "opera" collide with ordinary vocabulary and need their
# vendor qualifier.
_BROWSER_NAME_FRAGMENT = r"(?:chrome|chromium|firefox|safari|(?:microsoft|ms)\s+edge)"

# A browser token that is the OBJECT of a containment/usage verb is the
# host's bundled engine, not the execution environment (#1813 R110):
# "embeds Chromium runtime", "uses a Chromium runtime", "bundles
# Chromium for rendering". The span leaves before any environment
# reading, so the verbal forms behave like the fixed-phrase attachments
# ("powered by", "with").
_ENGINE_CONTAINMENT_RE = re.compile(
    rf"\b(?:embeds?|embedding|uses?|using|bundles?|bundling|includes?|"
    rf"including|contains?|containing|ships?|shipping|wraps?|wrapping|"
    rf"integrates?|integrating|carries?|carrying|packs?|packing|hosts?|"
    rf"hosting|loads?|loading)\s+"
    rf"(?:an?\s+|the\s+|its\s+)?(?:[\w\-'’]+\s+){{0,2}}?"
    rf"(?:{_BROWSER_NAME_FRAGMENT}|browsers?)\b(?:\s+[\w\-'’]+){{0,2}}"
)

# "SPA" is the canonical abbreviation of the single-page-application
# subtype (#1813 R115) — token-bounded so ordinary words stay words.
_WEB_APP_ARTIFACT_PHRASE_RE = re.compile(
    r"\b(?:web[\s\-]?app(?:lication)?s?|webapps?|websites?|web\s+uis?|frontends?|"
    r"front[\s\-]ends?|single[\s\-]page\s+app(?:lication)?s?|spas?|pwas?)\b"
)
_UI_PRODUCT_HEAD_FRAGMENT = (
    r"(?:apps?|applications?|webapps?|uis?|interfaces?|pages?|"
    r"frontends?|sites?|websites?|dashboards?|consoles?|portals?|"
    r"players?|editors?|viewers?|clients?|panels?|forms?)"
)
# A browser/web token re-headed by a subject/event or measurement noun
# is topic vocabulary, not an execution qualifier (#1813 R69/R73):
# "browser crash dashboard" reports on crashes, "browser admin console"
# is a console.
_SUBJECT_COMPOUND_NOUN_FRAGMENT = (
    r"(?:crash(?:es)?|security|incidents?|errors?|bugs?|outages?|"
    r"vulnerabilit(?:y|ies)|compatibility|latency|usage|adoption|"
    r"traffic|statistics|stats|metrics|performance|behaviors?|"
    r"behaviours?|trends?|share|history|activity|analytics|telemetry|"
    r"logs?|findings?|events?|data)"
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
    # A competing environment qualifier in the same NP voids the browser
    # qualifier (#1813 R74): "local desktop browser cookie dashboard" is
    # a desktop artifact whose browser word modifies its topic.
    + rf"(?:(?!(?:desktop|native|mobile|terminal|offline|embedded)\b){_NP_MODIFIER_TOKEN}\s+){{0,4}}?"
    # Bare "web" is a qualifier only in this bounded form — directly
    # modifying a UI product head ("web dashboard", #1813 R60); it stays
    # out of the general signal vocabulary.
    + rf"(?:{_WEB_APP_GOAL_SIGNAL_FRAGMENT}|web[\s\-]based|in[\s\-]browser|web)"
    # A subject/event noun after the qualifier re-heads it into topic
    # vocabulary (#1813 R73): "browser crash dashboard" reports on
    # crashes; "browser admin console" stays a console.
    + rf"(?:[\s\-]+(?!{_SUBJECT_COMPOUND_NOUN_FRAGMENT}\b){_NP_MODIFIER_TOKEN}){{0,3}}?"
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
    # The R45 "for browser use" idiom is a TERMINAL form (#1813 R69):
    # "use" is consumed only as the phrase's own end, so "browser usage
    # metrics" stays subject matter.
    r"browsers?\b(?:\s+use\b)?"
    # The environment phrase must END at the browser token (#1813 R46): a
    # trailing noun re-heads it into an artifact ("the browser extension",
    # "the browser automation suite"); function words keep the
    # environment reading.
    r"(?!\s+(?!(?:and|or|nor|but|without|with|for|on|in|at|by|"
    r"from|via|through|to|so|that|which|because|since|while|when|where|"
    r"using|during|after|before|only|instead|rather|too|as|alongside)\b)[\w'’\-]+)"
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
    r"denied|denylisted|off[\s\-]limits|out\s+of\s+scope|"
    r"outside\s+(?:the\s+)?scope|"
    r"not\s+in\s+scope|beyond\s+(?:the\s+)?scope)\b"
    # A denial predicate followed by a bare noun is that noun's
    # premodifier, not the artifact's state (#1813 R105): "excludes
    # unsupported browsers" states the app's compatibility policy —
    # function words and adverbs may follow, an object may not.
    r"(?!\s+(?!(?:and|or|nor|but|on|in|for|at|by|from|via|through|to|so|"
    r"that|which|because|since|while|when|where|during|after|before|"
    r"anymore|either|though|yet|now|currently|entirely|completely|"
    r"altogether|everywhere|here|there)\b)(?![\w'’]+ly\b)[\w'’\-]+)"
)

# Bare "web" takes the similarity reading too (#1813 R117):
# "web-style forms" compares, it does not produce.
_WEB_SIMILARITY_MODIFIER_RE = re.compile(
    rf"\b(?:{_WEB_APP_GOAL_SIGNAL_FRAGMENT}|web)[\s\-]+"
    r"(?:like|style|styled|esque|inspired|themed|free|flavou?red)\b"
)
# One shared component vocabulary across first-NP, goal-identity, and
# runtime-identity decisions (#1813 R83).
_COMPONENT_ARTIFACT_FRAGMENT = (
    r"(?:extensions?|plugins?|add[\s\-]?ons?|addons?|sidebars?|popups?|"
    r"pop[\s\-]ups?|toolbars?|overlays?|menus?|devtools?|drivers?|"
    # Action surfaces are components only in their QUALIFIED forms
    # (#1813 R86/R87): "browser action", "page action" — bare "actions"
    # is ordinary prose ("tracks user actions").
    r"automation|(?:browser|page)\s+actions?)"
)
# A browser/web token followed by a component-artifact noun is re-headed
# by that component (#1813 R60): a "browser extension settings page" is
# an extension surface, not a standalone browser application, so the
# browser word carries no web-product evidence. The component noun stays
# behind for the other matchers.
_BROWSER_COMPONENT_RE = re.compile(
    rf"\b(?:web|browsers?)[\s\-]+(?={_COMPONENT_ARTIFACT_FRAGMENT}\b|actions?\b)"
)

_EXPLICIT_WEB_VOCAB_RE = re.compile(
    r"\b(?:web[\s\-]?app(?:lication)?s?|webapps?|websites?|web\s+uis?|"
    r"frontends?|front[\s\-]ends?|single[\s\-]page\s+app(?:lication)?s?|spas?|pwas?)\b"
    r"|\bweb[\s\-]based\b|\bin[\s\-]browser\b"
)
_BARE_ADJACENT_QUALIFIER_RE = re.compile(
    rf"\b(?:browsers?|web|{_BROWSER_NAME_FRAGMENT})[\s\-]+"
    r"(?:apps?|applications?|webapps?|uis?|interfaces?|pages?|"
    r"frontends?|sites?|websites?|dashboards?|consoles?|portals?|"
    r"players?|editors?|viewers?|clients?|panels?|forms?)\b"
)
# Interaction-class heads act on their modifier ("vector-scene editor")
# and keep bare-qualifier ownership; display-class heads (dashboard,
# console, portal) naturally take topics and need section confirmation.
_INTERACTION_HEAD_TAIL_RE = re.compile(
    r"\b(?:apps?|applications?|webapps?|pages?|panels?|forms?|"
    r"players?|editors?|viewers?|clients?)[\s.!?]*$"
)


_NON_BROWSER_RUNTIME_RE = re.compile(
    r"\b(?:desktop|native|terminal|shell|mobile|ios|android|electron|"
    r"batch|cron|headless|processes?|local(?:ly)?|python|node|jvm|java)\b"
)


# A browser can consume an artifact without being that artifact's
# execution environment (#1813 R117/R118): PDFs, documents, and email
# artifacts are commonly viewed, opened, rendered, displayed, or
# presented in a browser. Strip these passive-consumer relations,
# including coordinated consumers ("in Chrome and Firefox", "in
# browsers and email clients"); declarations such as "runs in browsers"
# and "accessible in browsers" remain authoritative browser-runtime
# evidence.
_RUNTIME_BROWSER_CONSUMER_RE = re.compile(
    rf"\b(?:viewed|opened|previewed|read|rendered|displayed|presented|"
    rf"shown|printed|downloaded|exported|attached)\s+"
    rf"(?:directly\s+)?(?:in|on|through|using|by|via)\s+"
    rf"(?:(?:an?|the|modern|common|standard|supported|major|desktop|mobile)\s+)*"
    rf"(?:{_BROWSER_NAME_FRAGMENT}|browsers?)\b"
    rf"(?:\s*(?:,\s*(?:and|or)\b|,|\band\b|\bor\b|/)\s*"
    rf"(?:(?:an?|the|modern|common|standard|supported|major|desktop|mobile)\s+)*"
    rf"(?:{_BROWSER_NAME_FRAGMENT}|browsers?)\b){{0,4}}"
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
    # An engine attachment names the host's implementation, not the
    # execution environment (#1813 R106): a desktop app "powered by
    # Chromium" is hosted natively — the non-browser host dominates.
    r"(?<!powered by )(?<!built on )(?<!based on )(?<!backed by )"
    r"(?<!using )(?<!wrapping )(?<!wraps )(?<!atop )(?<!on top of )"
    # A bare accompaniment preposition directly before the token names a
    # bundled engine, not the environment (#1813 R108): "Electron
    # desktop runtime with Chromium" ships Chromium inside the host.
    r"(?<!with )(?<!via )(?<!through )"
    # A grouping adverbial ("aggregated ... by browser") names how data
    # is organized, not where the product runs (#1813 R116).
    r"(?<!by )"
    rf"\b(?:{_WEB_APP_GOAL_SIGNAL_FRAGMENT}|{_BROWSER_NAME_FRAGMENT})\b"
    r"(?!\s+(?!(?:and|or|nor|but|use|usage|without|with|for|on|in|at|by|"
    r"from|via|through|to|so|that|which|because|since|while|when|where|"
    r"using|during|after|before|only|instead|rather|too|as|alongside|"
    r"runtime|context|execution|environment|sessions?|windows?|tabs?)\b)[\w'’\-]+)"
)

_GOAL_COMPONENT_IDENTITY_RE = re.compile(
    r"\b(?:(?:is|are|was|were|becomes?)|[\w\-'’]+s\s+as|"
    r"[\w\-'’]+(?:ed|ing)\s+as|(?<!well\s)as(?=\s+(?:an?|the|our|my|your|their|its)\b))\s+"
    r"(?:(?:an?|the|our|my|your|their|its)\s+)?"
    r"(?:(?!(?:not|no|never)\b)(?![\w\-'’]+ing\b)[\w\-'’]+\s+){0,3}?"
    rf"{_COMPONENT_ARTIFACT_FRAGMENT}\b"
    r"(?!\s+(?!(?:and|or|nor|but|without|with|for|on|in|at|by|from|via|"
    r"through|to|so|that|which)\b)[\w'’\-]+)"
)
# ASSOCIATION relations re-head only a generic surface (#1813 R83): an
# explicit web artifact before the relation is an independent
# co-product, audience, or content owner and keeps its class.
_GOAL_COMPONENT_TARGET_RE = re.compile(
    r"\b(?:(?:[\w\-'’]+(?:ed|ing)\s+)?(?:for|of|in|inside|within|into|"
    # Bare "to" carries identity only toward a determined NP ("attached
    # to a browser extension"); an infinitive purpose ("to manage
    # browser extensions") names managed content (#1813 R81).
    r"to(?=\s+(?:an?|the|our|my|your|their|its)\b)|"
    r"by|alongside|beside)|[\w\-'’]+(?:ed|ing)\s+"
    # "hosted/mounted ON", "running/nested UNDER" (#1813 R71).
    r"(?:through|with|from|on|onto|under|beneath|atop)"
    # A bare host preposition reaches the component when its object is a
    # SINGULAR instance (#1813 R72/R74).
    r"|(?:on|onto|under|beneath|atop)(?=\s+(?:(?:an?|the|our|my|your|their|its)\s+)?"
    r"(?:[\w\-'’]+\s+){0,3}?"
    r"(?:extension|plugin|add[\s\-]?on|addon|sidebar|popup|toolbar|overlay|devtool|driver)\b(?!s)))\s+"
    r"(?:(?:an?|the|our|my|your|their|its)\s+)?"
    # A gerund in the target NP marks the component as managed content
    # ("for configuring browser extensions"), whatever the activity —
    # structural, no verb enumeration (#1813 R81/R82).
    r"(?:(?!(?:not|no|never)\b)(?![\w\-'’]+ing\b)[\w\-'’]+\s+){0,3}?"
    rf"{_COMPONENT_ARTIFACT_FRAGMENT}\b"
    r"(?!\s+(?!(?:and|or|nor|but|without|with|for|on|in|at|by|from|via|"
    r"through|to|so|that|which)\b)[\w'’\-]+)"
)

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
# An activity noun after a component word marks the component as the
# SUBJECT of the artifact's function (#1813 R62/R68): "plugin management
# dashboard" manages plugins; "extension settings page" belongs to the
# extension.
_COMPONENT_ACTIVITY_WORDS = frozenset(
    [
        "management",
        "monitoring",
        "reporting",
        "analytics",
        "administration",
        "tracking",
        "inventory",
        "catalog",
        "marketplace",
        "registry",
        "store",
        "listing",
        "listings",
        "usage",
        "statistics",
        "stats",
        "metrics",
        "insights",
        "overview",
        "comparison",
        "audit",
        "audits",
        "review",
        "reviews",
        "discovery",
        "search",
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
        "framework",
        "frameworks",
        "runner",
        "runners",
        "worker",
        "workers",
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

# Document/message formats retain their artifact identity even when a UI
# noun occurs later in the same first NP (#1813 R118): "interactive PDF
# form" is still a PDF, and "HTML email template" is still an email.
# Explicit web-product ownership is resolved before this fallback, so
# "PDF editor web app" and "browser-based PDF editor" remain web apps.
_NON_UI_ARTIFACT_WORDS = frozenset(
    [
        "pdf",
        "pdfs",
        "document",
        "documents",
        "ebook",
        "ebooks",
        "e-book",
        "e-books",
        "email",
        "emails",
        "e-mail",
        "e-mails",
    ]
)


def _goal_first_np_words(goal_text: str) -> tuple[str, ...] | None:
    """Walk the goal's first noun phrase and return its words.

    The walk mirrors the first-NP grammar: structural prepositions and
    relativizers end the phrase, a second determiner opens an embedded
    noun phrase, and participles are verbal outside phrase-initial
    position and the nominal-gerund vocabulary. Returning the full phrase
    lets artifact identity consider nouns before the final head. Returns
    ``None`` when no product NP exists — a bare-verb walk ("Migrate from
    ..."), or a qualified component compound ("browser extension settings
    page", "Chrome plugin popup page"), whose surfaces belong to the
    component (#1813 R61-R63/R118)."""
    np_tokens: list[str] = []
    seen_determiner = False
    content_since_determiner = 0
    first_segment = re.split(r"[.;:!?,]", goal_text)[0]
    segment_tokens = [token.lower() for token in re.findall(r"[\w'’\-]+", first_segment)]
    for position, lowered in enumerate(segment_tokens):
        # Component ownership is word-order independent (#1813 R62/R68):
        # a component noun owns its surfaces — "extension settings page",
        # "Chrome plugin popup page", possessive "extension's" — UNLESS
        # the following token is an ACTIVITY noun, in which case the
        # component is the subject of what the artifact does ("plugin
        # management dashboard").
        possessive_base = re.sub(r"[’']s$", "", lowered)
        # Action surfaces are components only when qualified by a
        # browser/page word (#1813 R86/R87).
        if possessive_base in ("action", "actions") and position > 0:
            previous = segment_tokens[position - 1]
            if previous in ("browser", "browsers", "page"):
                return None
        if possessive_base in _COMPONENT_NOUN_WORDS:
            if possessive_base != lowered:
                return None
            following = segment_tokens[position + 1] if position + 1 < len(segment_tokens) else ""
            if following not in _COMPONENT_ACTIVITY_WORDS:
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
    return tuple(np_tokens)


def _goal_first_np_head(goal_text: str) -> str | None:
    """Return the final head of the goal's first product noun phrase."""
    words = _goal_first_np_words(goal_text)
    return words[-1] if words else None


def _goal_first_np_has_ui_shape(goal_text: str) -> bool:
    """Output composition corroborates ownership; it cannot create it
    (#1813 R55). Default-allow with a non-product denylist (#1813 R63):
    legitimate UI product nouns need no pre-enumeration ("kanban board",
    "spreadsheet"), while report/tool/component-class heads keep their
    own artifact identity."""
    words = _goal_first_np_words(goal_text)
    return bool(
        words
        and words[-1] not in _NON_PRODUCT_HEAD_WORDS
        and not _NON_UI_ARTIFACT_WORDS.intersection(words)
    )


def _goal_first_np_is_ui_headed(goal_text: str) -> bool:
    """Affirmative UI-product head for cross-class suppression (#1813
    R61/R63): suppressing cli/service goal evidence demands the stricter
    claim that the phrase is HEADED by a UI product noun ("a CLI
    documentation website"), not merely that its head is unlisted ("a
    command line habit tracker")."""
    head = _goal_first_np_head(goal_text)
    return head is not None and bool(_UI_HEAD_NOUN_RE.match(head))


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
_WEBHOOK_SIGNAL_FRAGMENT = (
    r"(?:webhooks?|callback\s+urls?|http\s+posts?|incoming\s+events?|event\s+payloads?)"
)
# An adversative but/yet followed by an article introduces the affirmed
# pivot product after a denial (#1813 R122).
_ADVERSATIVE_PIVOT_RE = re.compile(r"\b(?:but|yet)\s+(?=(?:an?|the)\s+)")
# Webhook ownership needs affirmative receiver/input semantics (#1813
# R121): a receiving-artifact compound, a receiving verb governing the
# payload vocabulary, an incoming/inbound qualifier, or the delivery
# nouns themselves — a bare topic word owns nothing.
_WEBHOOK_RECEIVER_RE = re.compile(
    r"\b(?:webhooks?|callback\s+urls?)\s+"
    r"(?:receivers?|endpoints?|listeners?|handlers?|consumers?|processors?|ingestion|intake)\b"
    r"|\b(?:receiv\w+|ingest\w+|handl\w+|process\w+|consum\w+|accept\w+|listen\w+)\b"
    r"[^,.;]*\b(?:webhooks?|callback\s+urls?|http\s+posts?|incoming\s+events?|event\s+payloads?)\b"
    r"|\b(?:incoming|inbound)\s+(?:webhooks?|http\s+posts?|events?|payloads?)\b"
    r"|\bwebhooks?\s+(?:posts?|payloads?|deliveries|events?)\b"
)
