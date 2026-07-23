"""Execution-facing acceptance criteria normalization for auto-generated Seeds."""

from __future__ import annotations

import re

from ouroboros.core.seed import (
    AcceptanceCriterionInput,
    AcceptanceCriterionSpec,
    Seed,
    ac_text,
    derive_semantic_ac_key,
)

_AUTO_WRAPPER_CRITERIA = frozenset(
    {
        "`ooo auto` is dispatched to the mcp tool `ouroboros_auto`",
        "`ooo auto` is handled by ouroboros auto/mcp, not plain text",
        "final report includes auto session id, seed id, seed path, and test result",
        "final report includes auto session id, seed id, files changed, exact test command, and test result",
        "manual fallback is not used",
        "manual fallback was not used",
        "manual fallback used: no",
        "manual fallback used: false",
        "previous blocker recurrence is reported",
        "previous blocker recurrence: no",
        "previous last_question blocker did not recur",
        "previous seed grade c blocker did not recur",
        "previous interview closure blocker did not recur",
        "recursive auto invocation did not occur",
        "recursive auto invocation occurred: no",
        "report whether recursive auto invocation occurred",
    }
)

_OBSERVATION_REPORT_ONLY_CRITERIA = frozenset(
    {
        "`ooo auto` is dispatched through the installed ouroboros mcp tool, not interpreted as plain text",
        "`ooo auto` is dispatched to the mcp tool `ouroboros_auto`",
        "`ooo auto` is handled by ouroboros auto/mcp, not plain text",
        "whether mcp dispatch succeeded",
        "seed reaches grade a",
        "execution is handed off to the background execution job",
        "the execution job reaches a terminal status without manual cancellation",
        "whether progress accounting stalled at ac 0/n is reported",
        "execution job id",
        "final execution job terminal status",
        "whether manual fallback was used",
        "whether previous blockers recurred",
        "auto session id",
        "seed id and seed path",
        "files changed",
        "exact test command",
        "test result",
    }
)

_OBSERVATION_CONTEXT_REQUIRED = (
    "hello_auto.py",
    "tests/test_hello_auto.py",
)

_OBSERVATION_CONTEXT_ALTERNATES = (
    "ooo auto",
    "ouroboros_auto",
)

_CANONICAL_HELLO_AUTO_OBSERVATION_AC = (
    "Create `hello_auto.py` and `tests/test_hello_auto.py` so "
    "`hello_auto() -> str` returns exactly `{return_value}`, "
    "the test imports `hello_auto` and asserts that exact value, and "
    "the exact command `uv run pytest tests/test_hello_auto.py` passes."
)

_SEED_REPAIRER_ORIGINAL_REQUIREMENT_PREFIX = (
    "a command/api check returns stable observable output or artifacts proving "
    "the original requirement for "
)

_HELLO_AUTO_RETURN_EQUIVALENTS = frozenset(
    {
        "`hello_auto.py` defines `hello_auto()` returning exactly `hello from ooo auto`",
        "`hello_auto.py` defines `hello_auto() -> str` returning exactly `hello from ooo auto`",
        "hello_auto.py defines hello_auto() returning exactly hello from ooo auto",
        "hello_auto.py defines hello_auto() -> str returning exactly hello from ooo auto",
    }
)

_HELLO_AUTO_TEST_FILE_EQUIVALENTS = frozenset(
    {
        "`tests/test_hello_auto.py` imports `hello_auto` and asserts the exact return value",
        "tests/test_hello_auto.py imports hello_auto and asserts the exact return value",
        "tests/test_hello_auto.py imports hello_auto and asserts exact return value",
    }
)

_HELLO_AUTO_PYTEST_EQUIVALENTS = frozenset(
    {
        "`uv run pytest tests/test_hello_auto.py` passes",
        "uv run pytest tests/test_hello_auto.py passes",
        "the exact command `uv run pytest tests/test_hello_auto.py` passes",
        "the targeted test command `uv run pytest tests/test_hello_auto.py` passes",
    }
)

_HELLO_AUTO_EXISTENCE_EQUIVALENTS = frozenset(
    {
        "`hello_auto.py` exists",
        "hello_auto.py exists",
        "`tests/test_hello_auto.py` exists",
        "tests/test_hello_auto.py exists",
    }
)

_HELLO_AUTO_OBSERVATION_UNIT_EQUIVALENTS = (
    _HELLO_AUTO_RETURN_EQUIVALENTS
    | _HELLO_AUTO_TEST_FILE_EQUIVALENTS
    | _HELLO_AUTO_PYTEST_EQUIVALENTS
    | _HELLO_AUTO_EXISTENCE_EQUIVALENTS
)

_LIBRARY_DEFAULT_AC_EQUIVALENTS = frozenset(
    {
        "all public api symbols are importable from the documented module path",
        "all public api symbols importable from the documented module path",
        "unit tests cover every public function/method's primary success path",
        "`ruff check` and the project's type-check command exit 0",
        "ruff check and the project's type-check command exit 0",
    }
)

_LIBRARY_CONTEXT_SIGNALS = (
    "library",
    "package",
    "api surface",
    "public api",
    "sdk",
    "importable",
)

_FILE_ARTIFACT_SIGNALS = (
    " file ",
    " file named ",
    " exists",
    " content",
    " full content",
    " single line",
    " exact",
)

_AUTORESEARCH_CONTEXT_SIGNALS = (
    "autoresearch",
    "train.py",
    "val_bpb",
)

_AUTORESEARCH_CANONICAL_AC = (
    "The experiment ledger artifact contains a baseline entry written before any edit; it includes measured command `/usr/bin/time -l uv run train.py`, inner command, exit status, val_bpb, maximum resident set size bytes, and baseline status.",
    "The experiment ledger artifact contains at most two train.py-only experiment entries, each evaluated with the same measured command and timeout budget.",
    "The experiment ledger artifact contains sequential decision entries; each entry includes keep/revert status from the current best state, keeping strict val_bpb improvements and reverting ties, regressions, invalid runs, timeouts, crashes, missing metrics, missing memory, and unauthorized scope changes before the next attempt.",
    "Every baseline and experiment ledger artifact entry includes command, changed files, diff summary, observed val_bpb, memory, status, and keep/discard conclusion.",
    "The final git diff artifact contains only train.py changes unless scope_widening_ledger contains an explicit justification for a wider edit.",
    "The final report artifact includes baseline val_bpb, each attempted experiment result, final best val_bpb, and the keep/discard reason for every candidate.",
)


def _autoresearch_canonical_position(text: str) -> int | None:
    """Return the fixed canonical index of ``text``, or None if not canonical."""
    stripped = text.strip()
    for index, canonical in enumerate(_AUTORESEARCH_CANONICAL_AC):
        if canonical.strip() == stripped:
            return index
    return None


_AUTORESEARCH_NON_GOALS = (
    "Do not edit prepare.py.",
    "Do not edit files outside train.py unless scope_widening_ledger explicitly widens scope.",
    "Do not install dependencies, change package metadata, or modify the evaluation harness.",
    "Do not run training during Seed creation.",
)


def normalize_execution_acceptance(seed: Seed) -> Seed:
    """Remove auto-observation/reporting criteria from execution Seeds.

    Auto observation prompts can include wrapper/reporting duties such as
    dispatch confirmation and final auto-session metadata. Those should not be
    handed to the execution worker as implementation ACs. To avoid mutating
    product requirements, only normalize the known hello_auto observation
    context.
    """
    criteria_with_specs = tuple(
        (ac, text) for ac in seed.acceptance_criteria if (text := ac_text(ac).strip())
    )
    criteria = tuple(text for _ac, text in criteria_with_specs)
    direction_context = "\n".join((seed.goal, *seed.constraints))
    if not criteria:
        return seed

    filtered = criteria
    if _has_auto_wrapper_context(seed.goal, criteria):
        filtered = normalize_observation_execution_criteria(
            filtered, context_text=direction_context
        )
    filtered = normalize_file_artifact_execution_criteria(
        filtered,
        context_text=direction_context,
    )
    normalized_seed = seed
    if _has_autoresearch_context(direction_context, filtered):
        filtered = normalize_autoresearch_execution_criteria(
            filtered,
            context_text=direction_context,
        )
        normalized_seed = _with_autoresearch_seed_extras(normalized_seed, direction_context)
    if not filtered or (filtered == criteria and normalized_seed is seed):
        return seed
    if filtered != criteria:
        data = normalized_seed.to_dict()
        data["acceptance_criteria"] = list(
            _restore_surviving_acceptance_specs(filtered, criteria_with_specs)
        )
        normalized_seed = Seed.from_dict(data)
    return normalized_seed


def _has_explicit_semantic_key(criterion: AcceptanceCriterionSpec) -> bool:
    """Return whether a criterion's semantic key was explicitly supplied.

    ``Seed`` auto-derives a key for every criterion from its description and
    contract, so a key that equals ``derive_semantic_ac_key`` carries no author
    intent.  A key that *differs* from the derived value is an explicit identity
    that runtime routing and recovery correlate events on — canonicalization
    must never destroy it.
    """
    return (
        criterion.semantic_ac_key is not None
        and criterion.semantic_ac_key != derive_semantic_ac_key(criterion)
    )


def _carries_explicit_contract(criterion: AcceptanceCriterionInput) -> bool:
    """Return whether a criterion holds authority a rewrite must never drop.

    ``Seed`` materializes every criterion — including legacy strings — into an
    ``AcceptanceCriterionSpec`` with an auto-derived ``semantic_ac_key``.  That
    derived key is not, on its own, a contract.  But an explicit verification
    command/artifact/assertion, a declared investment, *or* an explicitly
    supplied semantic identity all carry authority a canonicalizing rewrite
    would silently discard.
    """
    return isinstance(criterion, AcceptanceCriterionSpec) and (
        criterion.has_success_contract
        or criterion.investment is not None
        or _has_explicit_semantic_key(criterion)
    )


def _identity_signature(criterion: AcceptanceCriterionSpec) -> tuple[object, ...]:
    """Return the full identity a collapse must not conflate.

    Two contract-bearing sources are safe to collapse only when both their
    verification contract *and* their explicit identity match; either differing
    makes them independent criteria.
    """
    return (
        criterion.verify_command,
        criterion.expected_artifacts,
        criterion.output_assertion,
        criterion.investment,
        criterion.semantic_ac_key if _has_explicit_semantic_key(criterion) else None,
    )


def _restore_surviving_acceptance_specs(
    filtered: tuple[str, ...],
    original: tuple[tuple[AcceptanceCriterionInput, str], ...],
) -> tuple[AcceptanceCriterionInput, ...]:
    """Reattach structured contracts to canonicalized criteria without loss.

    Normalization rewrites known-equivalent criteria to a canonical text.  That
    rewrite must never conflate, drop, or reorder a criterion's verification
    evidence or explicit identity.  The restoration therefore preserves three
    properties simultaneously:

    - **Identity** — a source carrying an explicit contract or an explicitly
      supplied ``semantic_ac_key`` is never merged away or stripped.  A canonical
      text that would conflate *distinct* identities refuses the collapse and
      keeps each source verbatim.  Bare/legacy equivalents (auto-derived keys,
      no contract) still collapse to one plain string.
    - **Order** — every emitted criterion is anchored to its originating source
      position and re-sorted, so a refused collapse or an autoresearch
      replacement can never move a contract ahead of or behind its siblings
      (sequential execution reads tuple order as stage order).
    - **Loss-free** — a caller-authored criterion is never deleted to satisfy a
      downstream constraint. Two criteria that arrive sharing a description keep
      both contracts; a canonical text seen twice never manufactures a phantom
      contractless duplicate.
    """
    remaining_indices = list(range(len(original)))

    def _pop(index: int) -> tuple[AcceptanceCriterionInput, str]:
        remaining_indices.remove(index)
        return original[index]

    # Emissions carry a ``(tier, position)`` sort key drawn from ONE coordinate
    # system so injected and sourced anchors never mix:
    #   tier 0 = normalizer-injected criteria (e.g. the fixed autoresearch
    #            canonical ACs), ordered by their filtered position so the
    #            canonical baseline/experiment sequence is never reordered;
    #   tier 1 = source-derived criteria, ordered by their originating source
    #            index so caller order is preserved.
    # Injected criteria therefore always precede source-only criteria, and a
    # contract transferred onto an injected canonical AC keeps that AC's tier-0
    # position rather than jumping into the source coordinate space.
    _SortKey = tuple[int, float]
    emissions: list[tuple[_SortKey, AcceptanceCriterionInput]] = []
    emitted_source_texts: set[str] = set()

    def _emit(key: _SortKey, item: AcceptanceCriterionInput) -> None:
        emissions.append((key, item))
        emitted_source_texts.add(ac_text(item).strip())

    for filtered_pos, text in enumerate(filtered):
        canonical_indices = [
            index
            for index in remaining_indices
            if isinstance(original[index][0], AcceptanceCriterionSpec)
            and _structured_criterion_normalizes_to(original[index][1], text)
        ]
        if canonical_indices:
            identity_specs = [
                original[index][0]
                for index in canonical_indices
                if _carries_explicit_contract(original[index][0])
            ]
            distinct_identities = {
                _identity_signature(spec)  # type: ignore[arg-type]
                for spec in identity_specs
            }
            if len(distinct_identities) > 1:
                # Conflating distinct identities would strand every command but
                # the first behind one canonical description.  Refuse the
                # collapse and keep EVERY source verbatim at its own position —
                # both the distinct contracts and the bare requirement the
                # canonical text would otherwise represent.  Dropping a bare
                # sibling here silently deletes a caller-authorized requirement.
                for index in list(canonical_indices):
                    criterion, _text = _pop(index)
                    _emit((1, float(index)), criterion)
                continue
            anchor = float(min(canonical_indices))
            specs = [_pop(index)[0] for index in canonical_indices]
            _emit(
                (1, anchor),
                _collapse_canonical_acceptance_specs(
                    specs,  # type: ignore[arg-type]
                    description=text,
                ),
            )
            continue

        exact_indices = [index for index in remaining_indices if original[index][1] == text]
        if exact_indices:
            # Group EVERY source with this exact text, not just the first, so
            # byte-identical repeats collapse into one execution AC (one command,
            # one key) while genuinely distinct identities each survive.  A source
            # whose text IS an autoresearch canonical AC anchors to the fixed
            # canonical sequence (tier 0); otherwise it stays in source order.
            specs = [_pop(index)[0] for index in exact_indices]
            canonical_position = _autoresearch_canonical_position(text)
            key: tuple[int, float] = (
                (0, float(canonical_position))
                if canonical_position is not None
                else (1, float(min(exact_indices)))
            )
            for item in _collapse_exact_match_criteria(specs):
                _emit(key, item)
            continue

        # A filtered text with no remaining source.  Either the normalizer
        # injected it (e.g. an autoresearch canonical AC) or it repeats a text an
        # earlier iteration already consumed and emitted.  Emit it only when it
        # is genuinely new — repeating an already-emitted description here would
        # manufacture a phantom contractless duplicate for the same requirement.
        if text.strip() in emitted_source_texts:
            continue
        _emit((0, float(filtered_pos)), text)

    # Never let a canonicalization path drop an explicit contract/identity: any
    # source a normalizer replaced or dropped (e.g. the autoresearch rewrite the
    # matcher above does not model) is restored at its source anchor.
    autoresearch = bool(filtered) and _AUTORESEARCH_CANONICAL_AC[0] in filtered
    for index in list(remaining_indices):
        criterion, source_text = original[index]
        if not _carries_explicit_contract(criterion):
            continue
        if autoresearch:
            subject = _unwrap_seed_repairer_original_requirement(source_text).strip()
            covered, canonical_index = _autoresearch_coverage(subject)
            # A structured source subsumed by a canonical AC transfers its contract
            # ONTO that canonical criterion; otherwise the string normalizer already
            # emitted the source's unwrapped requirement (bare), so the contract is
            # transferred onto THAT emission — never appended as a second copy.
            # Either way the requirement runs exactly once, contracted.
            if covered and canonical_index is not None:
                target = _AUTORESEARCH_CANONICAL_AC[canonical_index]
            else:
                target = subject
            transferred = _build_transferred_spec(criterion, target)  # type: ignore[arg-type]
            _pop(index)
            if not _place_transferred_onto_emission(emissions, target, transferred):
                emissions.append(((1, float(index)), transferred))
            continue
        _pop(index)
        emissions.append(((1, float(index)), criterion))

    emissions.sort(key=lambda item: item[0])
    # Normalization is loss-free: it never deletes a distinct caller-authorized
    # contract to force description uniqueness. Two criteria that arrive sharing
    # a description keep both contracts; the description-keyed evaluation map is
    # a pre-existing boundary limitation for such inputs, not something to fix by
    # discarding a valid Seed contract here.
    return tuple(emission for _key, emission in emissions)


def _collapse_exact_match_criteria(
    specs: list[AcceptanceCriterionInput],
) -> list[AcceptanceCriterionInput]:
    """Collapse same-description sources into one AC per distinct identity.

    All inputs share one description.  Byte-identical full identities (same
    contract and explicit key) collapse to a single execution AC — otherwise the
    same command would dispatch twice under one shared key.  A bare source (no
    contract, no explicit identity) is subsumed once any contract-bearing source
    is present, since the contracted criterion is the richer form of the same
    requirement.  Genuinely distinct identities are each preserved.
    """
    contract_specs = [spec for spec in specs if _carries_explicit_contract(spec)]
    if not contract_specs:
        # All bare: one representative AC for the shared requirement.
        return [specs[0]]
    result: list[AcceptanceCriterionInput] = []
    seen_signatures: list[tuple[object, ...]] = []
    for spec in contract_specs:
        signature = _identity_signature(spec)  # type: ignore[arg-type]
        if signature not in seen_signatures:
            seen_signatures.append(signature)
            result.append(spec)
    return result


def _build_transferred_spec(
    source: AcceptanceCriterionSpec,
    description: str,
) -> AcceptanceCriterionSpec:
    """Return the source's contract under ``description`` with a safe identity.

    The spec keeps the source's verification evidence and investment authority;
    an explicitly-supplied ``semantic_ac_key`` is preserved (routing/recovery
    correlate on it), while an auto-derived key is dropped so ``Seed`` re-derives
    a fresh one from ``description`` rather than persisting a stale source key.
    """
    explicit_key = (
        source.semantic_ac_key
        if source.semantic_ac_key is not None
        and source.semantic_ac_key != derive_semantic_ac_key(source)
        else None
    )
    return AcceptanceCriterionSpec(
        description=description,
        semantic_ac_key=explicit_key,
        verify_command=source.verify_command,
        expected_artifacts=source.expected_artifacts,
        output_assertion=source.output_assertion,
        investment=source.investment,
    )


def _place_transferred_onto_emission(
    emissions: list[tuple[tuple[int, float], AcceptanceCriterionInput]],
    target_text: str,
    transferred: AcceptanceCriterionSpec,
) -> bool:
    """Attach a contract onto the already-emitted criterion for ``target_text``.

    Returns whether the source is now represented by an existing emission (so the
    caller may drop it).  The target — a canonical AC or the string normalizer's
    unwrapped passthrough of this same requirement — keeps its description and
    position and gains the contract, so the executor sees exactly one contracted
    AC instead of a bare requirement plus a duplicate contracted copy.

    An emission that already carries explicit authority is only treated as
    representing this source when their identities are equivalent; a *different*
    identity (distinct command, artifacts, assertion, investment, or explicit
    key) returns False so the caller preserves the source separately.  Comparison
    is on explicit identity — an auto-derived key is not intended identity, so two
    equivalent contracts differing only in a derived key still collapse once.
    """
    target = target_text.strip()
    for position, (key, item) in enumerate(emissions):
        if ac_text(item).strip() != target:
            continue
        if isinstance(item, AcceptanceCriterionSpec) and _carries_explicit_contract(item):
            return _autoresearch_identity_matches(item, transferred)
        emissions[position] = (key, transferred)
        return True
    return False


def _explicit_semantic_key(criterion: AcceptanceCriterionSpec) -> str | None:
    """Return the criterion's key only when it is an explicit (non-derived) one."""
    return criterion.semantic_ac_key if _has_explicit_semantic_key(criterion) else None


def _autoresearch_identity_matches(
    existing: AcceptanceCriterionSpec,
    candidate: AcceptanceCriterionSpec,
) -> bool:
    """Return whether two canonical-AC criteria carry an equivalent identity.

    Compares the verification contract plus the *explicit* semantic identity; a
    materialized auto-derived key is not intended identity, so two equivalent
    contracts that differ only in a derived key are still equivalent and collapse
    once rather than dispatching the same command twice.
    """
    return (
        existing.verify_command == candidate.verify_command
        and existing.expected_artifacts == candidate.expected_artifacts
        and existing.output_assertion == candidate.output_assertion
        and existing.investment == candidate.investment
        and _explicit_semantic_key(existing) == _explicit_semantic_key(candidate)
    )


def _collapse_canonical_acceptance_specs(
    specs: list[AcceptanceCriterionSpec],
    *,
    description: str,
) -> AcceptanceCriterionInput:
    """Collapse sources with an equivalent identity into one canonical AC.

    Callers guarantee the sources hold at most one distinct explicit
    contract/identity, so this never merges conflicting evidence.  The lone
    contract (if any) is kept under the canonical description and its own
    semantic identity; ``expected_artifacts`` are an order-preserving union so
    equivalent contracts stay byte-identical.

    A collapse of purely bare legacy strings — no contract, no explicit identity
    on the *source* — degrades to a plain string so ``Seed`` re-derives a fresh
    key from the canonical description.  The degrade decision is made on the
    original identity, not the rewritten copy: changing the description would
    otherwise make an auto-derived key diverge from its derived value and be
    mistaken for an explicit identity, persisting a stale key.

    When a contract IS kept under the canonical description, an auto-derived key
    is re-derived from that description (an explicitly-supplied key is preserved),
    so canonical identity never depends on the collapsed source's wording.
    """
    identity = next(
        (spec for spec in specs if _carries_explicit_contract(spec)),
        specs[0],
    )
    if not _carries_explicit_contract(identity):
        return description
    artifacts: list[str] = []
    for spec in specs:
        for artifact in spec.expected_artifacts:
            if artifact not in artifacts:
                artifacts.append(artifact)
    rewritten = identity.model_copy(
        update={"description": description, "expected_artifacts": tuple(artifacts)}
    )
    if _has_explicit_semantic_key(identity):
        return rewritten
    return rewritten.model_copy(update={"semantic_ac_key": derive_semantic_ac_key(rewritten)})


def _structured_criterion_normalizes_to(original_text: str, normalized_text: str) -> bool:
    """Return whether one structured hello_auto AC became ``normalized_text``."""
    subject = _unwrap_seed_repairer_original_requirement(original_text).strip()
    normalized_subject = _normalize_known_observation_execution_line(subject)
    # ``_normalize_known_observation_execution_line`` returns its input unchanged
    # for anything it does not recognize as a hello_auto line.  Requiring an
    # actual transformation avoids a false identity match — e.g. an autoresearch
    # canonical AC equal to its own ``normalized_text`` must NOT be treated as a
    # hello collapse (which would route it into the source coordinate space and
    # invert the canonical sequence).
    if normalized_subject != subject and normalized_subject == normalized_text:
        return True
    return normalized_text.startswith(
        "Create `hello_auto.py` and `tests/test_hello_auto.py` so "
    ) and _is_hello_auto_observation_unit_line(original_text)


def normalize_observation_execution_criteria(
    criteria: tuple[str, ...],
    *,
    context_text: str = "",
) -> tuple[str, ...]:
    """Return concrete execution criteria for the hello_auto observation task.

    In the observation context, parent/reporting duties must not become worker
    ACs.  Keep only concrete local checks and canonicalize equivalent phrasings
    so the worker sees a small stable AC set.
    """
    if not _has_auto_wrapper_context(context_text, criteria):
        return criteria

    execution_lines: list[str] = []
    for criterion in criteria:
        stripped = criterion.strip()
        if not stripped:
            continue
        if is_auto_reporting_acceptance_criterion(stripped) or _is_observation_report_only_line(
            stripped
        ):
            continue
        if _is_observation_report_wrapper(stripped):
            continue
        execution_lines.append(stripped)

    if _has_complete_hello_auto_observation_unit(context_text, tuple(execution_lines)):
        passthrough = [
            line for line in execution_lines if not _is_hello_auto_observation_unit_line(line)
        ]
        canonical = _canonical_hello_auto_observation_ac(context_text, tuple(execution_lines))
        return tuple(dict.fromkeys((canonical, *passthrough)))

    normalized = [_normalize_known_observation_execution_line(line) for line in execution_lines]
    return tuple(dict.fromkeys(normalized))


def is_auto_reporting_acceptance_criterion(criterion: str) -> bool:
    """Return true only for exact known auto wrapper/report-only criteria.

    Broad observation-only report markers are intentionally handled behind the
    hello_auto observation context gate in ``normalize_observation_execution_criteria``.
    Keeping this standalone helper exact prevents unrelated product requirements
    such as execution-job or progress-accounting features from being classified
    as reporting metadata by a future caller that lacks the observation guard.
    """
    return _criterion_key(criterion) in _AUTO_WRAPPER_CRITERIA


def normalize_file_artifact_execution_criteria(
    criteria: tuple[str, ...],
    *,
    context_text: str = "",
) -> tuple[str, ...]:
    """Drop library defaults from direct file-artifact Seeds.

    When task-class inference falls back to ``library`` for a tiny file
    artifact, the catalog's import/unit-test/lint defaults are unrelated and
    can prevent ``ooo auto`` from reaching the requested runtime. Keep this
    scoped to file-state goals that do not explicitly ask for a library/API.
    """
    if not _has_file_artifact_context(context_text, criteria):
        return criteria

    filtered = tuple(
        criterion for criterion in criteria if not _is_library_default_acceptance(criterion)
    )
    return filtered or criteria


def normalize_autoresearch_execution_criteria(
    criteria: tuple[str, ...],
    *,
    context_text: str = "",
) -> tuple[str, ...]:
    """Return direct observable ACs for Karpathy-style autoresearch handoffs.

    The generic Seed repairer wraps vague ACs with
    ``A command/API check returns ...``. That wrapper is useful as a fallback
    for unknown tasks, but it violates the autoresearch plugin contract: the
    executor needs an experiment ledger contract, not a placeholder proof
    phrase. Keep this scoped to the plugin's distinctive train.py/val_bpb
    surface.
    """
    if not _has_autoresearch_context(context_text, criteria):
        return criteria
    passthrough: list[str] = []
    for criterion in criteria:
        subject = _unwrap_seed_repairer_original_requirement(criterion).strip()
        if not subject:
            continue
        if _is_autoresearch_generic_or_covered(subject):
            continue
        passthrough.append(subject)
    return tuple(dict.fromkeys((*_AUTORESEARCH_CANONICAL_AC, *passthrough)))


def has_auto_wrapper_context(text: str) -> bool:
    """Return true only for the known hello_auto observation prompt shape."""
    lowered = text.casefold()
    return all(marker in lowered for marker in _OBSERVATION_CONTEXT_REQUIRED) and any(
        marker in lowered for marker in _OBSERVATION_CONTEXT_ALTERNATES
    )


def _has_file_artifact_context(context_text: str, criteria: tuple[str, ...]) -> bool:
    context_lowered = f" {context_text} ".casefold()
    if any(signal in context_lowered for signal in _LIBRARY_CONTEXT_SIGNALS):
        return False
    text = f"{context_lowered}\n" + "\n".join(criteria).casefold()
    lowered = text.casefold()
    if not any(signal in lowered for signal in _FILE_ARTIFACT_SIGNALS):
        return False
    has_file_path = bool(re.search(r"\b[\w.-]+\.[A-Za-z0-9]{1,8}\b", lowered))
    has_file_check = any("exists" in criterion.casefold() for criterion in criteria)
    has_content_check = any(
        marker in criterion.casefold() for criterion in criteria for marker in ("content", "line")
    )
    return has_file_path and (has_file_check or has_content_check)


def _has_autoresearch_context(context_text: str, criteria: tuple[str, ...]) -> bool:
    text = "\n".join((context_text, *criteria)).casefold()
    return all(signal in text for signal in _AUTORESEARCH_CONTEXT_SIGNALS)


def _with_autoresearch_seed_extras(seed: Seed, context_text: str) -> Seed:
    data = seed.to_dict()
    runtime_context = data.get("runtime_context")
    if not isinstance(runtime_context, dict):
        runtime_context = {}
    defaults = {
        "repository_path": runtime_context.get("repository_path")
        or _extract_autoresearch_repository_path(context_text),
        "research_program": "program.md",
        "editable_files": ["train.py"],
        "fixed_files": ["prepare.py"],
        "verification_command": "uv run train.py",
        "measurement_command": "/usr/bin/time -l uv run train.py",
        "experiment_budget": 2,
        "timeout_seconds": 60,
        "primary_metric": "val_bpb",
        "metric_direction": "lower_is_better",
        "memory_source": "maximum resident set size from /usr/bin/time -l stderr, recorded as bytes.",
        "memory_heavy_threshold": "discard if experiment memory exceeds baseline by more than max(10% of baseline, 67108864 bytes).",
    }
    data["runtime_context"] = {**defaults, **runtime_context}
    data.setdefault("non_goals", list(_AUTORESEARCH_NON_GOALS))
    data.setdefault(
        "candidate_sequence",
        {
            "baseline_first": True,
            "sequential_from_current_best": True,
            "keep_rule": "keep only strict val_bpb improvements",
            "revert_rule": "revert discarded candidates before the next attempt",
        },
    )
    return Seed.from_dict(data)


def _extract_autoresearch_repository_path(context_text: str) -> str:
    for pattern in (
        r"repository(?: root)?:\s*([^\n]+)",
        r"work in repository:?\s*([^\n]+)",
    ):
        match = re.search(pattern, context_text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip().strip("`")
    return ""


# Demonstrable equivalence, not fuzzy matching: each entry is the *exact*
# normalized text (via ``_criterion_key``) of a standard autoresearch
# requirement restatement, mapped to the canonical AC (index into
# ``_AUTORESEARCH_CANONICAL_AC``) that provably expresses the same requirement,
# or ``None`` when it is folded into the Seed's runtime-context extras.  Token
# membership cannot establish equivalence — "record a baseline after every
# experiment" contradicts the canonical before-edit baseline while using only
# in-domain words — so only these exact phrasings collapse.  Any variation,
# added clause, or contradiction is not equivalent and is preserved verbatim.
_AUTORESEARCH_KNOWN_COVERED: dict[str, int | None] = {
    "seed has explicit runtime context, non-goals, and acceptance criteria "
    "as first-class content for the autoresearch contract": None,
    "seed preserves explicit runtime context, non-goals, and acceptance criteria "
    "as first-class content for the autoresearch contract": None,
    "seed requires execution to record a baseline uv run train.py result "
    "before any experiment changes evaluated": 0,
    # Spans experiment count (canonical index 1) AND sequential keep/revert
    # semantics (canonical index 2); no single canonical AC is one-to-one
    # equivalent, so a contracted source is preserved verbatim rather than
    # transferred onto the narrower count-only AC.  A bare restatement is still
    # covered — the canonical set collectively expresses it.
    "seed requires up to two post-baseline experiments to selected sequentially "
    "from the current best state, with improvements kept and all non-improvements "
    "reverted before the next attempt": None,
    "seed requires every baseline and experiment ledger entry to report command, "
    "changed files, diff summary, observed val_bpb, memory, status, and "
    "keep/discard conclusion": 3,
    "seed requires final kept changes to limited to train.py unless explicit scope "
    "widening recorded in the ledger": 4,
    "seed defines discard behavior for ties, regressions, invalid runs, missing "
    "val_bpb, missing memory, timeouts, memory-heavy behavior, nonzero exits, and "
    "unauthorized file changes": 2,
}


def _autoresearch_coverage(subject: str) -> tuple[bool, int | None]:
    """Classify an autoresearch source against the canonical contract.

    Returns ``(is_covered, canonical_index)``.  A source is covered only when its
    normalized text *exactly* matches a known standard requirement restatement,
    so a canonical AC demonstrably expresses the same requirement.  Anything
    else — an added clause, reworded or contradicting requirement, or a novel
    requirement — is not equivalent and is preserved.  ``canonical_index`` is the
    AC that subsumes a covered source, letting a structured source transfer its
    verification contract onto that canonical criterion rather than being dropped
    (losing evidence) or duplicated.
    """
    key = _criterion_key(subject)
    if key in _AUTORESEARCH_KNOWN_COVERED:
        return (True, _AUTORESEARCH_KNOWN_COVERED[key])
    return (False, None)


# Exact normalized text of the seed-repairer's generic placeholder fallbacks.
# Matching by substring instead would delete distinct requirements that merely
# open with the same proof phrase (e.g. "... while preserving the raw stderr
# log"), so only these exact placeholders are treated as generic.
_AUTORESEARCH_GENERIC_FALLBACKS: frozenset[str] = frozenset(
    {
        "a command/api check returns stable observable output or artifacts proving the task goal",
        "a command/api check returns stable observable output or artifacts",
    }
)


def _is_autoresearch_generic_or_covered(criterion: str) -> bool:
    key = _criterion_key(criterion)
    if key.startswith(_SEED_REPAIRER_ORIGINAL_REQUIREMENT_PREFIX):
        return True
    if key in _AUTORESEARCH_GENERIC_FALLBACKS:
        return True
    covered, _canonical_index = _autoresearch_coverage(criterion)
    return covered


def _is_library_default_acceptance(criterion: str) -> bool:
    subject = _unwrap_seed_repairer_original_requirement(criterion)
    key = _criterion_key(subject)
    return key in _LIBRARY_DEFAULT_AC_EQUIVALENTS


def _has_auto_wrapper_context(goal: str, criteria: tuple[str, ...]) -> bool:
    return has_auto_wrapper_context("\n".join((goal, *criteria)))


def _criterion_key(criterion: str) -> str:
    return " ".join(criterion.casefold().strip().rstrip(".").split())


def _normalize_known_observation_execution_line(criterion: str) -> str:
    """Canonicalize only known-equivalent hello_auto execution AC phrasings."""
    key = _criterion_key(criterion)
    if key in _HELLO_AUTO_RETURN_EQUIVALENTS:
        return (
            "`hello_auto.py` defines `hello_auto() -> str` returning exactly `hello from ooo auto`."
        )
    if key in _HELLO_AUTO_TEST_FILE_EQUIVALENTS:
        return "`tests/test_hello_auto.py` imports `hello_auto` and asserts the exact return value."
    if key in _HELLO_AUTO_PYTEST_EQUIVALENTS:
        return "The exact command `uv run pytest tests/test_hello_auto.py` passes."
    return criterion


def _canonical_hello_auto_observation_ac(context_text: str, criteria: tuple[str, ...]) -> str:
    return_value = _extract_hello_auto_return_value("\n".join((context_text, *criteria)))
    return _CANONICAL_HELLO_AUTO_OBSERVATION_AC.format(return_value=return_value)


def _extract_hello_auto_return_value(text: str) -> str:
    for pattern in (
        r"hello_auto\(\)(?:\s*->\s*str)?\s+returns?\s+exactly\s+[`'\"]([^`'\"]+)[`'\"]",
        r"must\s+return\s+exactly\s+[`'\"]([^`'\"]+)[`'\"]",
        r"returning\s+exactly\s+[`'\"]([^`'\"]+)[`'\"]",
    ):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return "hello from ooo auto"


def _is_observation_report_only_line(criterion: str) -> bool:
    """Classify exact known observation metadata lines from the parent report."""
    return _criterion_key(criterion) in _OBSERVATION_REPORT_ONLY_CRITERIA


def _has_complete_hello_auto_observation_unit(
    context_text: str,
    criteria: tuple[str, ...],
) -> bool:
    """Return true when the observation asks for the full proof+pytest unit."""
    text = "\n".join((context_text, *criteria)).casefold()
    return (
        "hello_auto.py" in text
        and "tests/test_hello_auto.py" in text
        and "hello from ooo auto" in text
        and "uv run pytest tests/test_hello_auto.py" in text
    )


def _is_hello_auto_observation_unit_line(criterion: str) -> bool:
    """Classify lines that are part of the canonical hello_auto smoke unit."""
    subject = _unwrap_seed_repairer_original_requirement(criterion)
    return _criterion_key(subject) in _HELLO_AUTO_OBSERVATION_UNIT_EQUIVALENTS


def _is_observation_report_wrapper(criterion: str) -> bool:
    """Return true for repairer-wrapped observation report requirements."""
    key = _criterion_key(criterion)
    if not key.startswith(_SEED_REPAIRER_ORIGINAL_REQUIREMENT_PREFIX):
        return False
    return "observation report" in key or "plain chat summary" in key


# Case-insensitive, whitespace-tolerant matcher for the seed-repairer wrapper
# prefix.  Stripping via this regex (rather than the case-folded criterion key)
# preserves the original casing of the wrapped requirement — a repaired
# requirement referencing case-sensitive identifiers like ``RawStderr.LOG`` or
# ``VAL_BPB`` must round-trip verbatim, not lowercased.
_SEED_REPAIRER_WRAPPER_RE = re.compile(
    r"^\s*"
    + re.escape(_SEED_REPAIRER_ORIGINAL_REQUIREMENT_PREFIX.strip()).replace(r"\ ", r"\s+")
    + r"\s+",
    re.IGNORECASE,
)


def _unwrap_seed_repairer_original_requirement(criterion: str) -> str:
    """Strip every nested seed-repairer wrapper, preserving the inner casing.

    The repairer can wrap an already-wrapped criterion, so unwrapping must
    recurse until the innermost requirement is exposed; otherwise a nested
    wrapper is later matched as a generic placeholder and the real requirement
    it carries is deleted.  Casing is preserved so case-sensitive identifiers
    survive round-trip.
    """
    text = criterion.strip()
    while True:
        stripped = _SEED_REPAIRER_WRAPPER_RE.sub("", text, count=1).strip()
        if stripped == text:
            return text
        text = stripped
