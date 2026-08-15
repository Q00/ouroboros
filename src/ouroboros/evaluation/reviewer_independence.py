"""Executor != reviewer binding for formal evaluation (PR-X X2).

Cross-model consensus already requires >=2 distinct voters, but nothing stopped
the *executor's own vendor* from also being the *reviewer*. A meta-harness can do
better: it knows which runtime backend produced the artifact, so it can keep that
vendor out of the jury. This module is the pure, deterministic policy for that.

Two questions, both answered here without any I/O:

1. Which voter models should we drop because they share the executor's vendor?
   (:func:`filter_voter_models` — best-effort, never drops below a viable jury.
   Voters whose vendor cannot be mapped are always kept: an unknown vendor
   cannot be *proven* same-vendor any more than it can be proven different.)
2. What honest independence label should the result carry?
   (:func:`resolve_reviewer_independence`.)

The four honest labels, in decision order:

* ``unavailable`` — fewer than two distinct vendors are configured on this
  machine, so no independent reviewer exists to select. No behavior change.
* ``independent`` — at least one retained voter has a *known* vendor that
  differs from the executor's vendor. Unknown vendors are never evidence of
  independence: sentinel model names like Codex's ``"default"`` mean "the
  CLI's own default model", which may well be the executor's vendor.
* ``unverified`` — independence could be neither proven nor disproven: no
  known-different voter exists, but the roster contains unknown-vendor voters
  (or the executor's own vendor is unmappable). An honest "we don't know",
  distinct from a false ``independent`` or a false ``same_vendor`` claim.
* ``same_vendor`` — every voter's vendor is known and shares the executor's
  vendor (e.g. filtering would have broken quorum, so the roster was kept).

Single-backend setups are the common case: with only one vendor configured there
is no independent reviewer to be had, so the label is ``unavailable`` and nothing
changes — exactly the "no behavior change for a single-backend setup" contract.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from ouroboros.backends import get_backend_capability

# Minimum voters a consensus jury needs to remain meaningful. We never filter a
# roster below this, even to gain independence — votes beat purity.
_MIN_VIABLE_VOTERS = 2

# Honest independence labels stamped onto the evaluation result meta.
# See the module docstring for the full decision semantics of each label.
INDEPENDENT = "independent"
SAME_VENDOR = "same_vendor"
UNAVAILABLE = "unavailable"
UNVERIFIED = "unverified"

# Canonical runtime-backend -> vendor family. Backends that share a model vendor
# collapse to the same family so a claude executor is not "independent" of a
# claude_mcp/ourocode reviewer.
_BACKEND_VENDOR: dict[str, str] = {
    "claude": "anthropic",
    "claude_mcp": "anthropic",
    "ourocode": "anthropic",
    "codex": "openai",
    "codex_mcp": "openai",
    "copilot": "openai",
    "gemini": "google",
    "antigravity": "google",
    "grok": "xai",
    "opencode": "opencode",
    "hermes": "hermes",
    "kiro": "kiro",
    "goose": "goose",
    "pi": "pi",
    "gjc": "gjc",
}

# Substring heuristics for mapping a *model* string to a vendor family. Checked
# in order; the first hit wins. Covers both bare ("gpt-4o") and namespaced
# ("openrouter/anthropic/claude-3.5") model identifiers.
_MODEL_VENDOR_MARKERS: tuple[tuple[str, str], ...] = (
    ("anthropic", "anthropic"),
    ("claude", "anthropic"),
    ("openai", "openai"),
    ("gpt", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("google", "google"),
    ("gemini", "google"),
    ("xai", "xai"),
    ("grok", "xai"),
    ("mistral", "mistral"),
    ("llama", "meta"),
    ("meta", "meta"),
    ("cohere", "cohere"),
)

_UNKNOWN_VENDOR = "unknown"


@dataclass(frozen=True, slots=True)
class ReviewerIndependence:
    """Verdict on whether the reviewer jury is independent of the executor."""

    status: str
    executor_vendor: str | None
    voter_vendors: tuple[str, ...]
    filtered_voters: tuple[str, ...]

    @property
    def is_independent(self) -> bool:
        """True when at least one voter is a different vendor than the executor."""
        return self.status == INDEPENDENT


def backend_vendor(backend: str | None) -> str | None:
    """Map a runtime backend name to its vendor family, or ``None`` if unknown."""
    if not backend or not backend.strip():
        return None
    name = backend.strip().lower()
    if name in _BACKEND_VENDOR:
        return _BACKEND_VENDOR[name]
    # Fall back to the capability registry's canonical name before giving up.
    capability = get_backend_capability(name)
    if capability is not None and capability.name in _BACKEND_VENDOR:
        return _BACKEND_VENDOR[capability.name]
    return None


def model_vendor(model: str | None) -> str:
    """Map a model identifier to its vendor family (``"unknown"`` if unmappable)."""
    if not model or not model.strip():
        return _UNKNOWN_VENDOR
    lowered = model.strip().lower()
    for marker, vendor in _MODEL_VENDOR_MARKERS:
        if marker in lowered:
            return vendor
    return _UNKNOWN_VENDOR


def _distinct_configured_vendors(configured_backends: Iterable[str]) -> set[str]:
    vendors: set[str] = set()
    for backend in configured_backends:
        vendor = backend_vendor(backend)
        if vendor is not None:
            vendors.add(vendor)
    return vendors


def filter_voter_models(
    voter_models: Sequence[str],
    executor_backend: str | None,
) -> tuple[str, ...]:
    """Drop voters *known* to share the executor's vendor, preserving a viable jury.

    Best-effort: if excluding same-vendor voters would shrink the jury below
    :data:`_MIN_VIABLE_VOTERS`, the original roster is kept unchanged (a jury
    that can actually vote beats a perfectly-independent one that cannot).

    Unknown-vendor voters are never dropped: an unmappable model (e.g. Codex's
    ``"default"`` sentinel) cannot be proven same-vendor, so removing it would
    discard a potentially-independent vote on no evidence.
    """
    executor_vendor = backend_vendor(executor_backend)
    models = tuple(voter_models)
    if executor_vendor is None or not models:
        return models
    # ``unknown`` vendors survive this comparison by design (see docstring).
    kept = tuple(m for m in models if model_vendor(m) != executor_vendor)
    if len(kept) >= _MIN_VIABLE_VOTERS:
        return kept
    return models


def resolve_reviewer_independence(
    executor_backend: str | None,
    voter_models: Sequence[str],
    configured_backends: Sequence[str],
) -> ReviewerIndependence:
    """Classify reviewer independence and return the (post-filter) voter roster.

    Labels (full semantics in the module docstring):

    * ``unavailable`` — fewer than two distinct vendors are configured on this
      machine, so there is no independent reviewer to select. No behavior change.
    * ``independent`` — at least one retained voter has a *known* vendor that
      differs from the executor's vendor. Unknown vendors provide NO independence
      evidence: a sentinel like Codex's ``"default"`` resolves to the CLI's own
      default model, which may be the executor's vendor.
    * ``unverified`` — independence is unprovable either way: no known-different
      voter, but the roster carries unknown-vendor voters (or the executor's own
      vendor is unmappable).
    * ``same_vendor`` — every voter vendor is known and matches the executor's.
    """
    executor_vendor = backend_vendor(executor_backend)
    configured_vendors = _distinct_configured_vendors(configured_backends)

    if len(configured_vendors) < 2:
        voters = tuple(voter_models)
        return ReviewerIndependence(
            status=UNAVAILABLE,
            executor_vendor=executor_vendor,
            voter_vendors=tuple(sorted({model_vendor(m) for m in voters})),
            filtered_voters=voters,
        )

    filtered = filter_voter_models(voter_models, executor_backend)
    filtered_vendors = tuple(sorted({model_vendor(m) for m in filtered}))
    has_unknown = _UNKNOWN_VENDOR in filtered_vendors
    # Independence must be POSITIVELY proven: a voter whose vendor is known and
    # different from the executor's. ``unknown`` never counts as different.
    has_known_different = executor_vendor is not None and any(
        vendor != executor_vendor and vendor != _UNKNOWN_VENDOR for vendor in filtered_vendors
    )
    if has_known_different:
        status = INDEPENDENT
    elif has_unknown or executor_vendor is None:
        status = UNVERIFIED
    else:
        status = SAME_VENDOR
    return ReviewerIndependence(
        status=status,
        executor_vendor=executor_vendor,
        voter_vendors=filtered_vendors,
        filtered_voters=filtered,
    )


__all__ = [
    "INDEPENDENT",
    "SAME_VENDOR",
    "UNAVAILABLE",
    "UNVERIFIED",
    "ReviewerIndependence",
    "backend_vendor",
    "filter_voter_models",
    "model_vendor",
    "resolve_reviewer_independence",
]
