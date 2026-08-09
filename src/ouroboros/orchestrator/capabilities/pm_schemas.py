"""Schemas for the PM interview's advisory lanes (RFC Q00/ouroboros#1937).

The PM interview fans out the same way the regular interview does, with two
deliberate differences that both fall out of one sentence: *a confirmed fact may
occupy a round of its own; an unconfirmed one may not become anything.*

**Two lanes, not six.** Only the lanes that fetch evidence carry over — what the
code does today, and what the data says. A lane that critiques the question or
narrows it into options produces a draft judgment, not evidence, and a draft
wears the face of material while standing in for the decision.

**One answer path, and no exception to its confirmation.** The regular
interview's code-fact contract carries an ``answer_prefix``, and a chain follows
it: a confirmation flag, an exception to that flag
(``[from-code][auto-confirmed]``), and a confidence grade deciding which answers
earn the exception. That exception is justified there — the person being asked
usually wrote the code — and PM has no such premise, so PM does not inherit it.

What PM inherits is the rest: the same ``answer_prefix`` field, under an enum
holding exactly one element. ``[from-code][auto-confirmed]`` is therefore not
forbidden here; it is unspellable. The prohibition is retired by removing the
place rather than by growing the list of things forbidden — the same move the
data lane made when a typed read request replaced its free-text query field
(#1754/#1825).

Two doors were the alternative, and the cost was measured. PM originally kept
no answer path at all and grew a second entrance for findings — an ``evidence``
parameter no sibling tool has. Two entrances meant two sets of rules for the
same payload, and seven review rounds found the same class of silent loss at a
new address each time. One door with a one-element enum is what closed it.

**What holds when a host skips the confirmation anyway.** The prompt is shown by
the host, so the contract can ask for it but not perform it. The guarantee that
does not depend on the host is downstream and structural: any answer opening
with ``[from-code]`` settles as ``provenance="observation"`` where it enters, so
requirement extraction reads :data:`~ouroboros.bigbang.answer_provenance.
WITHHELD_ANSWER_NOTE` in its place and completion counts only rounds whose
provenance is ``"user"``. An unconfirmed forward costs a question turn; it
cannot become a PRD requirement and cannot end the interview.

**The forwarded text is composed from the ``examined`` entries, not from a
free-text field.** There is deliberately no ``answer_text`` here. Every claim
the host forwards therefore carries the repository it was checked against — the
entry it sits in — and a repository-relative ``path``, which a prose field would
have let it shed.

That structure is also why the lane's scope cannot contradict its claims. Scope
and claim were two lists once, and an answer could declare one repository
examined while citing another's code; folding them into per-repository entries
made the contradiction unspellable rather than checked (RFC #1937 decision 7).

``data_context`` is reused verbatim from the interview. It already works this
way: no confirmation, no grade, closed answer states, and no ``[from-data]``
answer path — measurements are shown beside the question and the answer is the
user's own words. The new surface is exactly one contract, for the code lane.
"""

from __future__ import annotations

import hashlib
from pathlib import PurePath
import re
from typing import Any

from ouroboros.orchestrator.capabilities.interview_schemas import (
    _interview_data_evidence_answer_contract,
)
from ouroboros.orchestrator.capabilities.question_text import normalize_question_text


def stable_pm_question_identity(question: str) -> str:
    """Return a deterministic identity for a PM-interview question.

    A distinct prefix from the interview's, and both contracts pin their own
    with a pattern. The two tools can run against the same repository in the
    same session, and an identity that only says "a question" would let a PM
    lane's answer validate against an interview fan-out that asked something
    else -- the binding would be shaped correctly and mean nothing.
    """
    digest = hashlib.sha256(normalize_question_text(question).encode("utf-8")).hexdigest()[:16]
    return f"pm-question:{digest}"


#: Why the code lane read no repository at all. A closed set, for the same
#: reason the data lane's is closed: the reasons this lane can have are known in
#: advance, so this is a choice rather than a sentence.
#:
#: Every constant is scoped to the lane itself. A lane sees what reached it, not
#: what the PM has, so it is not positioned to say a policy does not exist.
#:
#: There is deliberately no "found nothing in what I read" constant here. That
#: is not a reason the lane reports, it is the shape of its answer: repositories
#: it read and found nothing in are entries carrying no claim, and a constant
#: restating that could disagree with the entries beside it.
PM_NOTHING_EXAMINED_REASONS: tuple[str, ...] = (
    "not_a_policy_question",
    "no_repository_in_roster",
    "roster_repository_not_readable",
)

#: Roster repository identifiers, constrained like identifiers -- no whitespace,
#: no quotes, no parentheses. Same shape and same reason as the data lane's
#: identifier pattern: a field with no shape is one a sentence can be written
#: into, and an evidence item whose repository is a sentence cannot be matched
#: against the roster by value.
_PM_REPO_ID_PATTERN = r"^[A-Za-z0-9_.:\-]{1,128}$"

#: How many policy claims one repository's entry may carry. A repository that
#: needs more than this is not making one policy claim, it is a file listing.
_PM_CLAIMS_MAX_PER_REPOSITORY = 20

#: How many repositories one answer may report on. Bounds the answer, not the
#: roster.
_PM_EXAMINED_MAX_REPOSITORIES = 64


def pm_repo_id(*, name: str | None, path: str) -> str:
    """Return the roster identifier for one repository.

    Two halves, and each is there for a different reason. The readable half is a
    slug of the repository's name so a PRD citation says something to the person
    reading it. The digest half is taken from the durable path, which is what
    the roster is keyed by, so the identifier stays the same when someone
    renames the repository and stays distinct when two repositories share a
    name -- the case a name-only identifier silently merges.

    The digest is of the path as given. Two spellings of the same checkout
    therefore produce two identifiers; that is a duplicate roster entry rather
    than a collision, and it is visible as one.
    """
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:8]
    readable = (name or "").strip() or PurePath(path).name
    slug = re.sub(r"[^A-Za-z0-9_.\-]+", "-", readable).strip("-.")[:96]
    return f"{slug}-{digest}" if slug else f"repo-{digest}"


def _pm_repo_id_property(description: str) -> dict[str, Any]:
    return {"type": "string", "pattern": _PM_REPO_ID_PATTERN, "description": description}


#: A path inside a repository, as one or more ``/``-separated segments where no
#: segment is ``.`` or ``..``.
#:
#: This says in the contract what the field's description used to say in prose,
#: and the difference is not stylistic. ``repo_id`` is closed against the roster,
#: so a citation's repository can be checked from the value alone; ``path`` was
#: left open, so ``/etc/passwd``, ``C:\src\x.cs``, ``\\server\share\x`` and
#: ``../../etc/shadow`` were all accepted and rendered to the PM underneath an
#: in-roster ``repo_id``.
#:
#: The harm is the evidence contract's own purpose. An absolute path names the
#: machine the lane ran on -- a CI checkout at ``/home/runner/work/...`` -- and
#: the PM looking for it in their own clone finds nothing, so the field built to
#: let them check the evidence is what stops them. A traversal is worse than
#: unusable: it points outside the repository while being filed as that
#: repository's file.
#:
#: The three rejected shapes fall out of the same requirement rather than being
#: enumerated as a blocklist: a leading ``/`` and a leading ``\`` cannot start a
#: segment, a drive letter is refused at the front, and ``.``/``..`` are refused
#: as whole segments wherever they appear. Control characters are excluded so a
#: newline cannot smuggle a second line into a rendered citation.
_PM_EVIDENCE_PATH_PATTERN = (
    r"^(?![A-Za-z]:)"
    r"(?:(?!\.\.?(?:/|$))[^/\\\x00-\x1f]+)"
    r"(?:/(?:(?!\.\.?(?:/|$))[^/\\\x00-\x1f]+))*$"
)


def _pm_policy_claim_schema() -> dict[str, Any]:
    """Return the schema for one policy claim inside a repository's entry.

    The claim carries no ``repo_id``. It used to, because the alternative was
    scoping the request instead, which leaves a citation's origin unstated: it
    cannot be traced back, out-of-roster evidence cannot be rejected from the
    value alone, and two repositories implementing different policies flatten
    into one answer text.

    Nesting the claim under its repository keeps all of that and removes what
    the identifier could still get wrong. A claim's repository is now the entry
    it sits in rather than a value it repeats, so it cannot name a repository
    the answer did not say it read. Disagreement still needs no field of its
    own: two entries carrying different ``policy_claim`` *is* the disagreement,
    in a shape the surface can render.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["path", "policy_claim"],
        "properties": {
            "path": {
                "type": "string",
                "minLength": 1,
                "maxLength": 512,
                "pattern": _PM_EVIDENCE_PATH_PATTERN,
                "description": (
                    "Path within this entry's repository, relative to its root. Relative "
                    "because an absolute path names the machine the lane ran on, "
                    "which is not what the PM is being asked to check. Enforced "
                    "here rather than asked for: absolute paths, Windows drive "
                    "and UNC paths, and any '.' or '..' segment are rejected at "
                    "re-entry."
                ),
            },
            "policy_claim": {
                "type": "string",
                "minLength": 1,
                "maxLength": 600,
                "description": (
                    "What this source shows the system does today. Descriptive "
                    "only: what exists, never what the PRD should decide."
                ),
            },
        },
    }


def _pm_examined_repository_schema(*, max_claims: int) -> dict[str, Any]:
    """Return the schema for one repository the lane read.

    One entry is one repository the lane actually opened, and it carries what
    was found there -- possibly nothing. That is the whole of the lane's scope
    reporting: a repository with no entry was not read, and an entry with no
    claims was read and had nothing. Neither needs a field to say so.

    ``max_claims`` is zero for the state that carries no policy at all, which is
    what makes the three answer states mutually exclusive from the entries
    alone rather than from a boolean the child sets beside them.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["repo_id", "policy_claims"],
        "properties": {
            "repo_id": _pm_repo_id_property(
                "Roster identifier of a repository this lane read. An identifier "
                "outside the roster, or one repeated in another entry, is "
                "rejected at re-entry."
            ),
            "policy_claims": {
                "type": "array",
                "maxItems": max_claims,
                "items": _pm_policy_claim_schema(),
                "description": (
                    "What this repository shows the system does today, one claim "
                    "per source. Empty means the lane read this repository and "
                    "found no policy bearing on the question -- which is a "
                    "different statement from the repository being absent, and "
                    "the reason both are spellable."
                ),
            },
        },
    }


def _pm_code_context_answer_contract() -> dict[str, Any]:
    """Return the answer contract for PM's ``code_context`` advisory lane.

    The lane id is the interview's, deliberately: it names a role — bring what
    the code says about this question — and both tools want that role. What
    differs is the contract, and a contract is what the catalog attaches per
    tool. A second lane id for the same role would have said the roles differ,
    which is the one thing that is not true here.

    **Scope and claim live in one place.** This lane first carried them in two
    lists -- ``examined_repository_ids`` for what it read, ``evidence[]`` for
    what it found -- and review round 8 showed an answer declaring only the API
    repository examined while citing the web repository's code, accepted as
    complete. Two lists about the same subject can always drift, and a check
    comparing them would have closed that one drift while leaving the shape that
    produced it. They are folded instead: ``examined`` is one list of
    per-repository entries, each carrying the claims found in that repository,
    so a claim's repository is the entry it sits in. Citing a repository outside
    the declared scope is not rejected here, it is unspellable.

    Three closed states, and the discriminator is the entries rather than a flag
    beside them. ``policy_found`` is gone for the reason the fold happened at
    all: a boolean restating what the entries already show is one more thing
    that can disagree with them. So is
    ``no_policy_found_in_examined_repositories`` -- entries with no claims say
    exactly that, and say which repositories it was true of.

    - ``NothingExamined`` -- no repository was read, and
      ``nothing_examined_reason`` says why. This is the only state carrying a
      reason, because it is the only one where the entries cannot speak.
    - ``NoPolicyInExaminedRepositories`` -- repositories were read and none
      implements policy bearing on the question. Every entry carries no claim,
      which is what keeps "found nothing across two of five" different from
      "found nothing across all five".
    - ``PolicyCarried`` -- at least one entry carries a claim, and the three
      forwarding fields are required. ``contains`` is what makes this state
      unreachable without a claim, so "confirm my finding" with nothing to
      confirm has no spelling, and neither has a claim that arrives with no
      confirmation asked for.

    ``answer_prefix`` exists only in the carried state, under an enum of one
    element, and there is no confidence grade beside it. The enum is the
    enforcement: a child cannot mark this output as skipping confirmation
    because ``[from-code][auto-confirmed]`` is not a value this field can hold,
    and the shape is closed, so it cannot add a field that says so another way.
    A rule forbidding the prefix would read as authoritative while being
    enforced by nothing -- three of #1825's findings were guarantees stated
    where nothing made them true. The other two states have no ``answer_prefix``
    property at all, for the same reason they carry no claim: there is nothing
    to forward.

    ``examined`` is required in all three states, empty list included. It is
    what keeps the lane's reporting about itself: "no policy found" means
    nothing about the PM's repositories until it says which ones were read, and
    a session whose roster failed to load reports an empty list rather than
    silently reading as "searched everywhere and found nothing". A repository
    the lane never opened simply has no entry, which is what makes stopping
    early -- the behaviour the lane brief asks for -- reportable honestly.
    """
    identity_property = {
        "type": "string",
        "pattern": r"^pm-question:[0-9a-f]{16}$",
        "description": "Binds this answer to the PM question that requested it.",
    }

    def examined_property(*, max_claims: int) -> dict[str, Any]:
        """The examined list for a state that read at least one repository.

        ``max_claims`` of zero is the state that carries nothing: every entry is
        then a repository read with no claim, and no ``contains`` is needed
        because none could be satisfied. Above zero it is the carried state, and
        ``contains`` demands that at least one entry actually carry a claim --
        which is what separates the two states from the entries alone.
        """
        prop: dict[str, Any] = {
            "type": "array",
            "minItems": 1,
            "maxItems": _PM_EXAMINED_MAX_REPOSITORIES,
            "items": _pm_examined_repository_schema(max_claims=max_claims),
            "description": (
                "Repositories this lane read, one entry each, carrying what it "
                "found there. A repository it did not open has no entry; an "
                "entry with no claims was read and had nothing."
            ),
        }
        if max_claims:
            prop["contains"] = {
                "type": "object",
                "required": ["policy_claims"],
                "properties": {"policy_claims": {"type": "array", "minItems": 1}},
            }
        return prop

    nothing_examined_state: dict[str, Any] = {
        "title": "NothingExamined",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "question_identity",
            "lane_id",
            "examined",
            "nothing_examined_reason",
        ],
        "properties": {
            "question_identity": identity_property,
            "lane_id": {"const": "code_context"},
            "examined": {
                "type": "array",
                "maxItems": 0,
                "description": (
                    "Empty: this lane opened no repository. What it did not read "
                    "it does not report on."
                ),
            },
            "nothing_examined_reason": {
                "type": "string",
                "enum": list(PM_NOTHING_EXAMINED_REASONS),
                "description": (
                    "Why nothing was read. Chosen from a closed set, and each one "
                    "is about this lane rather than about what the PM has -- a "
                    "subagent sees what reached it, not what is registered."
                ),
            },
        },
    }
    no_policy_state: dict[str, Any] = {
        "title": "NoPolicyInExaminedRepositories",
        "type": "object",
        "additionalProperties": False,
        "required": ["question_identity", "lane_id", "examined"],
        "properties": {
            "question_identity": identity_property,
            "lane_id": {"const": "code_context"},
            "examined": examined_property(max_claims=0),
        },
    }
    found_state: dict[str, Any] = {
        "title": "PolicyCarried",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "question_identity",
            "lane_id",
            "examined",
            "answer_prefix",
            "requires_user_confirmation",
            "user_confirmation_prompt",
        ],
        "properties": {
            "question_identity": identity_property,
            "lane_id": {"const": "code_context"},
            "examined": examined_property(max_claims=_PM_CLAIMS_MAX_PER_REPOSITORY),
            "answer_prefix": {
                "type": "string",
                "enum": ["[from-code]"],
                "description": (
                    "The prefix the host puts on this finding when the user "
                    "confirms it. One value, and that is the whole of the "
                    "confirmation policy: the interview's "
                    "'[from-code][auto-confirmed]' cannot be spelled here, so "
                    "no output of this lane can declare itself exempt from "
                    "being confirmed."
                ),
            },
            "requires_user_confirmation": {
                "const": True,
                "description": (
                    "Always true. Named as the interview names it so one host "
                    "code path serves both tools, and constant because the "
                    "premise that earns an exception there -- the person being "
                    "asked usually wrote the code -- is not a premise a PRD has."
                ),
            },
            "user_confirmation_prompt": {
                "type": "string",
                "minLength": 1,
                "maxLength": 600,
                "description": (
                    "What to ask the user before forwarding this finding. "
                    "Required, because a confirmation step with no text to show "
                    "is one the host is free to skip silently."
                ),
            },
        },
    }
    return {
        "contract_id": "pm_code_context_answer.v1",
        "scope": "single_pm_question_code_context",
        # Two things, and deliberately no third -- the schema re-entry enforces,
        # and the instruction the child must follow. A claim *about* the system
        # in a third field reads as authoritative as either and is enforced by
        # nothing (#1825).
        "response_model_schema": {"oneOf": [nothing_examined_state, no_policy_state, found_state]},
        "runtime_instruction": (
            "Read the repositories named in the roster you were given, and report "
            "what they implement today for this question. Describe, never "
            "prescribe: what the code does is an input to the PM's decision and "
            "is not the decision. Give every repository you actually read an entry "
            "in examined, carrying what you found there; a repository you read and "
            "found nothing in is an entry with no claims, and one you never opened "
            "has no entry. "
            "If two repositories implement different policies, carry both; the "
            "disagreement is what the PM most needs to see. Evidence from outside "
            "the roster is not evidence: say so in your finding so the PM can add "
            "the repository, and do not give it an entry. When you carry a "
            "policy, set answer_prefix to [from-code] and write "
            "user_confirmation_prompt as the question the user should be asked "
            "before your finding is recorded on their behalf -- the finding is "
            "recorded as an adopted fact, never as their decision, and the "
            "decision is what they write on the question that follows."
        ),
    }


def pm_code_context_answer_contract() -> dict[str, Any]:
    """Return the public ``code_context`` answer contract."""
    return _pm_code_context_answer_contract()


def _pm_question_advisory_fanout_metadata() -> dict[str, Any]:
    """Return structured metadata for per-question PM answer help.

    Both lanes are ``required``. A lane may be required only when it has a
    total answer -- one that exists no matter what the question is -- because a
    required lane with no such answer would block questions it has nothing to
    say about. Both have one: the data lane answers ``data_needed=false`` with a
    reason, and the code lane answers with an empty ``examined`` and a reason it
    read nothing. That
    is also why requiring them is worth doing: optional would let a question that
    did need evidence lose it silently, which is the defect the lanes exist to
    remove.
    """
    lanes = [
        {
            "lane_id": "code_context",
            "purpose": (
                "Report what the roster repositories implement today for this "
                "question, so the PM decides on top of current behaviour."
            ),
            "capability": "inspect_code",
            "required": True,
            "answer_contract": _pm_code_context_answer_contract(),
        },
        {
            "lane_id": "data_context",
            "purpose": (
                "Take the measurements that inform this question, so the PM "
                "judges against numbers instead of memory."
            ),
            "capability": "read_data",
            "required": True,
            # Reused verbatim. It already satisfies what PM needs: closed answer
            # states, no confirmation flag, no confidence grade, and reasons that
            # are statements about the lane rather than about the host.
            "answer_contract": _interview_data_evidence_answer_contract(),
        },
    ]
    return {
        "contract_id": "pm_question_advisory_fanout.v1",
        "mcp_tool": "ouroboros_pm_interview",
        "question_identity_prefix": "pm-question",
        "advisory_goal": "put_evidence_beside_the_pm_question",
        "payload_title_prefix": "PM advisory",
        "allowed_capabilities": ["inspect_code", "read_data"],
        "question_heading": "## PM Question",
        # The counterpart of the interview's, and the sentence that differs: no
        # quality of evidence lets a finding here skip the user.
        "task_preamble": (
            "You are an Ouroboros PM interview advisory subagent.\n"
            "\n"
            "The parent session has already shown the PM question to the user. "
            "Your job is to\nreport what the system does today. Never decide on "
            "behalf of the user, however\nclear your finding is: a PRD asks what "
            "the system should do and you can only\nreport what it does. Your "
            "finding is shown to the user for confirmation, and it\nis recorded "
            "as an adopted fact -- never as their decision."
        ),
        # Unconditional, unlike the interview's. There is no configuration of
        # evidence quality that lets a finding here reach the record without the
        # user seeing it, so the rule the child reads must not be phrased as a
        # condition it evaluates.
        "child_answer_rule": (
            "Never decide on behalf of the user, however clear your finding is. "
            "A PRD asks what the system should do and you can only report what "
            "it does. Your finding is confirmed by the user and then recorded as "
            "an adopted fact; the decision is what they write in their own words "
            "on the question that follows."
        ),
        "dispatch_timing": "after_question_is_visible_to_user",
        "parallel_preference": "parallel_when_runtime_supports_subagents",
        "sequential_fallback": {
            "supported": True,
            "mode": "sequential_advisory_lane_dispatch",
            "trigger": "runtime_has_no_native_parallel_subagent_primitive",
        },
        "lanes": lanes,
        "synthesis_contract": {
            # Not ``answer_advisory``. That shape asks for answer options and a
            # recommended draft, which is the draft judgment PM does not take
            # from a lane. What the PM receives is the evidence, next to the
            # question they are answering themselves.
            "output_shape": "evidence_beside_question",
            "include_recommended_draft": False,
            "preserve_user_agency": True,
        },
        "response_payload_refs": {
            "result_correlation_key": "context.lane_id",
            "requires_prose_parsing": False,
            "synthesis_owner": "parent_session",
        },
        "runtime_instruction": (
            "Show the PM question to the user first, then fan out the code-policy "
            "and data lanes. A code lane that carries a policy returns "
            "answer_prefix [from-code] and a confirmation prompt: show that "
            "prompt, and only if the user confirms, send the finding as the "
            "answer with its [from-code] prefix. It is recorded as an adopted "
            "fact -- withheld from requirement extraction, not counted toward "
            "completion -- and the server returns the next question, which is "
            "the one the user answers in their own words. There is no prefix "
            "that skips the confirmation."
        ),
    }


def pm_question_advisory_fanout_metadata() -> dict[str, Any]:
    """Return the public PM question-advisory fan-out metadata."""
    return _pm_question_advisory_fanout_metadata()


def _pm_orchestration_metadata() -> dict[str, Any]:
    """Return the whole orchestration block for ``ouroboros_pm_interview``.

    PM's advisory catalog travels the same road the interview's does: the tool
    capability registry, under ``orchestration.question_advisory_fanout``. That
    is what makes the fan-out machinery one implementation with two consumers
    rather than two implementations — the request builder, the payload builder
    and re-entry all read the catalog from the registry, keyed by tool name, and
    none of them needs to know which tool it is serving.

    The block is assembled here rather than in the registry module so the two
    tools' catalogs stay in their own files, and so adding a lane to PM never
    means editing a module shared with every other tool.
    """
    from ouroboros.orchestrator.capabilities.lateral_personas import (
        _pm_interview_subagent_metadata,
    )

    return {
        "pm_interview_subagent": _pm_interview_subagent_metadata(),
        "question_advisory_fanout": _pm_question_advisory_fanout_metadata(),
    }


def pm_repository_roster(repos: Any) -> list[dict[str, str]]:
    """Return the roster the code lane reads and cites, from persisted records.

    Accepts what the PM session actually persists: repo records, or bare path
    strings from the plugin dispatch path. Both become the same shape, because
    the lane is told the roster by value and a shape that varies by how the
    session was started is one the child has to guess at.

    **Where to read and what to call it are different paths.** A registered repo
    may be redirected to a snapshot worktree pinned to the remote default
    branch, so that a stale local checkout never reaches the PRD. ``path`` is
    therefore the snapshot when one exists -- it is where the lane reads -- while
    ``repo_id`` is derived from the durable checkout, so a citation survives the
    worktree being reclaimed and recreated.

    Deriving the identifier from the snapshot instead would make it change every
    time the snapshot moved, which is the one thing an identifier may not do:
    evidence submitted before the move would stop matching the roster it was
    bounded by.
    """
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    if not isinstance(repos, (list, tuple)):
        return entries
    for repo in repos:
        if isinstance(repo, str):
            read_path, durable_path, name, desc = repo, repo, "", ""
        elif isinstance(repo, dict):
            read_path = str(repo.get("path") or repo.get("source_path") or "")
            durable_path = str(repo.get("source_path") or repo.get("path") or "")
            name = str(repo.get("name") or "")
            desc = str(repo.get("desc") or "")
        else:
            continue
        if not read_path or durable_path in seen:
            continue
        seen.add(durable_path)
        entry = {"repo_id": pm_repo_id(name=name, path=durable_path), "path": read_path}
        if name:
            entry["name"] = name
        if desc:
            entry["desc"] = desc
        entries.append(entry)
    return entries


__all__ = [
    "PM_NOTHING_EXAMINED_REASONS",
    "_pm_code_context_answer_contract",
    "_pm_question_advisory_fanout_metadata",
    "pm_code_context_answer_contract",
    "pm_question_advisory_fanout_metadata",
    "pm_repo_id",
    "pm_repository_roster",
]
