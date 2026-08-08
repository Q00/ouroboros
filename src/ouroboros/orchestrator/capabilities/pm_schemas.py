"""Schemas for the PM interview's advisory lanes (RFC Q00/ouroboros#1937).

The PM interview fans out the same way the regular interview does, with two
deliberate differences that both fall out of one sentence: *a lane's output is
material for the PM's answer and can never become the answer.*

**Two lanes, not six.** Only the lanes that fetch evidence carry over — what the
code does today, and what the data says. A lane that critiques the question or
narrows it into options produces a draft judgment, not evidence, and a draft
wears the face of material while standing in for the decision.

**No answer path, so no confirmation.** The regular interview's code-fact
contract carries an ``answer_prefix``, and because a lane's output can be the
answer there, a chain follows it: a confirmation flag, an exception to it, and a
confidence grade deciding which answers earn the exception. That chain is
justified there — the person being asked usually wrote the code. PM has no such
premise, so the chain is not inherited. It is not replaced by a stricter version
of itself either: neither contract here has a field an answer could be spelled
in, which is why there is no rule saying one must not be. The same move the data
lane made when a typed read request replaced its free-text query field
(#1754/#1825) — the prohibition is retired by removing the place, not by growing
the list of things forbidden.

``data_context`` is reused verbatim from the interview. It already works this
way: no confirmation, no grade, closed answer states, and no ``[from-data]``
answer path. The new surface is exactly one contract, for the code lane.
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


#: Why the code lane carries no policy. A closed set, for the same reason the
#: data lane's is closed: the reasons this lane can have are known in advance,
#: so this is a choice rather than a sentence.
#:
#: Every constant is scoped to the lane itself or to what it examined. A lane
#: sees what reached it, not what the PM has, so it is not positioned to say a
#: policy does not exist -- only that it did not find one in the repositories it
#: read. ``no_policy_found_in_examined_repositories`` carries that scope in its
#: own name, and ``examined_repository_ids`` says which repositories that was.
PM_NO_POLICY_REASONS: tuple[str, ...] = (
    "not_a_policy_question",
    "no_repository_in_roster",
    "roster_repository_not_readable",
    "no_policy_found_in_examined_repositories",
)

#: Roster repository identifiers, constrained like identifiers -- no whitespace,
#: no quotes, no parentheses. Same shape and same reason as the data lane's
#: identifier pattern: a field with no shape is one a sentence can be written
#: into, and an evidence item whose repository is a sentence cannot be matched
#: against the roster by value.
_PM_REPO_ID_PATTERN = r"^[A-Za-z0-9_.:\-]{1,128}$"

#: How many evidence items one code-policy answer may carry. A policy answer
#: that needs more than this is not one policy claim, it is a file listing.
_PM_EVIDENCE_MAX_ITEMS = 20


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


def _pm_policy_evidence_schema() -> dict[str, Any]:
    """Return the schema for one piece of code-policy evidence.

    ``repo_id`` is on the evidence item rather than on the request because of
    what each placement can decide. Scoping the request says where to look; it
    leaves an evidence item's origin unstated, so a citation cannot be traced
    back, out-of-roster evidence cannot be rejected from the value alone, and
    two repositories implementing different policies flatten into one answer
    text. The disagreement is the most valuable PRD input available, and a
    contract whose only outlet is prose loses it.

    With the identifier here, disagreement needs no field of its own: two
    evidence items carrying different ``policy_claim`` under different
    ``repo_id`` *is* the disagreement, in a shape the surface can render.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["repo_id", "path", "policy_claim"],
        "properties": {
            "repo_id": _pm_repo_id_property(
                "Roster identifier of the repository this evidence came from. "
                "An identifier outside the roster is rejected at re-entry."
            ),
            "path": {
                "type": "string",
                "minLength": 1,
                "maxLength": 512,
                "pattern": _PM_EVIDENCE_PATH_PATTERN,
                "description": (
                    "Path within that repository, relative to its root. Relative "
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


def _pm_code_context_answer_contract() -> dict[str, Any]:
    """Return the answer contract for PM's ``code_context`` advisory lane.

    The lane id is the interview's, deliberately: it names a role — bring what
    the code says about this question — and both tools want that role. What
    differs is the contract, and a contract is what the catalog attaches per
    tool. A second lane id for the same role would have said the roles differ,
    which is the one thing that is not true here.

    Two closed states and no third, mirroring the data lane. The states are
    ``policy_found`` true or false, and each names everything the other cannot
    borrow -- an open shape with optional fields accepted "found a policy, here
    is why I found nothing", which is two answers in one object and neither
    checkable.

    There is no ``answer_prefix``, no ``requires_user_confirmation``, and no
    confidence grade. Their absence is the enforcement: a child cannot mark this
    output as the interview answer because the shape holds no field that says
    so, and the shape is closed, so it cannot add one. A rule forbidding it would
    read as authoritative while being enforced by nothing -- three of #1825's
    findings were guarantees stated where nothing made them true.

    ``examined_repository_ids`` is required in both states, empty list included.
    It is what keeps the lane's reporting about itself: "no policy found" means
    nothing about the PM's repositories until it says which ones were read, and
    a session whose roster failed to load reports an empty list rather than
    silently reading as "searched everywhere and found nothing".
    """
    identity_property = {
        "type": "string",
        "pattern": r"^pm-question:[0-9a-f]{16}$",
        "description": "Binds this answer to the PM question that requested it.",
    }
    examined_property = {
        "type": "array",
        "maxItems": 64,
        "items": _pm_repo_id_property("A roster repository this lane actually read."),
        "description": (
            "Roster repositories this lane read. Required even when empty: it is "
            "the scope every other statement in this answer is relative to."
        ),
    }
    no_policy_state: dict[str, Any] = {
        "title": "NoPolicyCarried",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "question_identity",
            "lane_id",
            "policy_found",
            "examined_repository_ids",
            "no_policy_reason",
        ],
        "properties": {
            "question_identity": identity_property,
            "lane_id": {"const": "code_context"},
            "policy_found": {
                "const": False,
                "description": (
                    "No policy is carried. Either the question is not asking what "
                    "the system does today, or nothing readable here implements it."
                ),
            },
            "examined_repository_ids": examined_property,
            "evidence": {"type": "array", "maxItems": 0},
            "no_policy_reason": {
                "type": "string",
                "enum": list(PM_NO_POLICY_REASONS),
                "description": (
                    "Why no policy is carried. Chosen from a closed set. Each one "
                    "is about this lane or about what it examined, never about "
                    "what the PM has -- a subagent sees what reached it, not what "
                    "is registered."
                ),
            },
        },
    }
    found_state: dict[str, Any] = {
        "title": "PolicyFound",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "question_identity",
            "lane_id",
            "policy_found",
            "examined_repository_ids",
            "evidence",
        ],
        "properties": {
            "question_identity": identity_property,
            "lane_id": {"const": "code_context"},
            "policy_found": {
                "const": True,
                "description": "The examined repositories implement policy bearing on this question.",
            },
            "examined_repository_ids": examined_property,
            "evidence": {
                "type": "array",
                "minItems": 1,
                "maxItems": _PM_EVIDENCE_MAX_ITEMS,
                "items": _pm_policy_evidence_schema(),
                "description": (
                    "What the code shows, one claim per source. Items from "
                    "different repositories that disagree are left as they are: "
                    "the contradiction is a PRD input, not a defect to reconcile."
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
        "response_model_schema": {"oneOf": [no_policy_state, found_state]},
        "runtime_instruction": (
            "Read the repositories named in the roster you were given, and report "
            "what they implement today for this question. Describe, never "
            "prescribe: what the code does is an input to the PM's decision and "
            "is not the decision. Name every repository you actually read in "
            "examined_repository_ids -- everything else you say is scoped to it. "
            "If two repositories implement different policies, carry both; the "
            "disagreement is what the PM most needs to see. Evidence from outside "
            "the roster is not evidence: say so in your finding so the PM can add "
            "the repository, and do not put it in evidence."
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
    reason, and the code lane answers ``policy_found=false`` with a reason. That
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
        # The counterpart of the interview's, and the sentence that differs: a
        # finding here is never the answer, under any evidence quality.
        "task_preamble": (
            "You are an Ouroboros PM interview advisory subagent.\n"
            "\n"
            "The parent session has already shown the PM question to the user. "
            "Your job is to\nput evidence beside that question. Never answer on "
            "behalf of the user, however\nclear your finding is: a PRD asks what "
            "the system should do and you can only\nreport what it does."
        ),
        # Unconditional, unlike the interview's. There is no configuration of
        # evidence quality that lets a finding here become the answer, so the
        # rule the child reads must not be phrased as a condition it evaluates.
        "child_answer_rule": (
            "Never answer on behalf of the user, however clear your finding is. "
            "A PRD asks what the system should do and you can only report what "
            "it does; your output is put beside the question, and the answer is "
            "whatever the user writes in their own words."
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
            "and data lanes. Put what they return beside the question as material "
            "for the user's judgment. There is no answer prefix to forward and no "
            "confirmation step: the answer is whatever the user writes in their "
            "own words, and a lane's finding never takes its place."
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
    "PM_NO_POLICY_REASONS",
    "_pm_code_context_answer_contract",
    "_pm_question_advisory_fanout_metadata",
    "pm_code_context_answer_contract",
    "pm_question_advisory_fanout_metadata",
    "pm_repo_id",
    "pm_repository_roster",
]
