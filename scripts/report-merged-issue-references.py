#!/usr/bin/env python3
"""Report open issues whose visible direct PR references are all merged.

GitHub's cross-reference timeline is the authority for the visible relationships;
this is evidence for human inspection, not an issue disposition. API failures,
missing source objects, and incomplete pages are indeterminate, never empty success.
"""

from __future__ import annotations

from collections.abc import Callable
import json
import subprocess
import sys
import time
from typing import Any

REPO = "Q00/ouroboros"
ISSUES = """query($cursor: String) {
  repository(owner: "Q00", name: "ouroboros") {
    issues(first: 100, after: $cursor, states: OPEN) {
      nodes { number }
      pageInfo { hasNextPage endCursor }
    }
  }
}"""
REFERENCES = """query($number: Int!, $cursor: String) {
  repository(owner: "Q00", name: "ouroboros") {
    issue(number: $number) {
      state
      timelineItems(first: 100, after: $cursor, itemTypes: [CROSS_REFERENCED_EVENT]) {
        nodes { ... on CrossReferencedEvent {
          willCloseTarget
          source { __typename ... on PullRequest {
            number state mergedAt isDraft repository { nameWithOwner }
          } }
        } }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}"""


class AuditError(Exception):
    """The audit could not establish a complete, trustworthy result."""


def graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    payload = json.dumps({"query": query, "variables": variables})
    for attempt in range(3):
        try:
            result = subprocess.run(
                ["gh", "api", "graphql", "--input", "-"],
                input=payload,
                capture_output=True,
                text=True,
                check=True,
                timeout=60,
            )
            response = json.loads(result.stdout)
            if not isinstance(response, dict) or response.get("errors"):
                raise AuditError("GitHub GraphQL returned errors")
            data = response.get("data")
            if not isinstance(data, dict):
                raise AuditError("GitHub GraphQL returned no data")
            return data
        except (subprocess.SubprocessError, OSError, json.JSONDecodeError, AuditError) as exc:
            if attempt == 2:
                raise AuditError("GitHub API read failed after three attempts") from exc
            time.sleep(2**attempt)
    raise AssertionError("unreachable")


def pages(fetch: Callable[[str | None], dict[str, Any]]) -> list[dict[str, Any]]:
    cursor = None
    seen: set[str] = set()
    nodes: list[dict[str, Any]] = []
    while True:
        connection = fetch(cursor)
        try:
            batch, info = connection["nodes"], connection["pageInfo"]
            more, next_cursor = info["hasNextPage"], info["endCursor"]
            if not isinstance(batch, list) or not isinstance(more, bool):
                raise ValueError
            if any(not isinstance(node, dict) for node in batch):
                raise ValueError
            nodes.extend(batch)
            if not more:
                return nodes
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen:
                raise ValueError
            seen.add(next_cursor)
            cursor = next_cursor
        except (KeyError, TypeError, ValueError) as exc:
            raise AuditError("GitHub returned an incomplete or repeated page") from exc


def audit(read: Callable[[str, dict[str, Any]], dict[str, Any]] = graphql) -> list[dict[str, Any]]:
    def issues_page(cursor: str | None) -> dict[str, Any]:
        try:
            return read(ISSUES, {"cursor": cursor})["repository"]["issues"]
        except (KeyError, TypeError) as exc:
            raise AuditError("Repository or issue connection unavailable") from exc

    issues = pages(issues_page)
    candidates = []
    seen_issues: set[int] = set()
    for issue in issues:
        number = issue.get("number")
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            raise AuditError("Missing issue identity")
        if number in seen_issues:
            raise AuditError("Repeated issue in paginated inventory")
        seen_issues.add(number)

        def reference_page(cursor: str | None) -> dict[str, Any]:
            try:
                current = read(REFERENCES, {"number": number, "cursor": cursor})["repository"][
                    "issue"
                ]
                if current["state"] != "OPEN":
                    raise AuditError("An issue changed state during the audit; retry")
                return current["timelineItems"]
            except (KeyError, TypeError) as exc:
                raise AuditError("Issue or timeline unavailable") from exc

        refs: dict[int, dict[str, Any]] = {}
        for event in pages(reference_page):
            source = event.get("source")
            closing = event.get("willCloseTarget")
            if not isinstance(source, dict) or not isinstance(closing, bool):
                raise AuditError(f"Issue #{number} has an unavailable reference")
            kind = source.get("__typename")
            if kind != "PullRequest":
                if kind != "Issue":
                    raise AuditError(f"Issue #{number} has an unknown reference type")
                continue
            repository = source.get("repository")
            if not isinstance(repository, dict) or not isinstance(
                repository.get("nameWithOwner"), str
            ):
                raise AuditError(f"Issue #{number} has a reference with unavailable repository")
            if repository["nameWithOwner"].casefold() != REPO.casefold():
                continue
            pr_number, state, merged, draft = (
                source.get(key) for key in ("number", "state", "mergedAt", "isDraft")
            )
            if (
                not isinstance(pr_number, int)
                or isinstance(pr_number, bool)
                or pr_number <= 0
                or state not in {"OPEN", "CLOSED", "MERGED"}
                or not isinstance(draft, bool)
                or (state == "MERGED" and (not isinstance(merged, str) or not merged or draft))
                or (state != "MERGED" and merged is not None)
            ):
                raise AuditError(f"Issue #{number} has an incomplete PR reference")
            previous = refs.get(pr_number)
            if previous and (previous["state"], previous["mergedAt"]) != (state, merged):
                raise AuditError(f"PR #{pr_number} changed during the audit; retry")
            refs[pr_number] = {
                "number": pr_number,
                "state": state,
                "mergedAt": merged,
                "closing": closing or (previous is not None and previous["closing"]),
            }
        if refs and all(pr["state"] == "MERGED" for pr in refs.values()):
            candidates.append(
                {
                    "issue": number,
                    "pull_requests": sorted(refs.values(), key=lambda pr: pr["number"]),
                }
            )
    return sorted(candidates, key=lambda candidate: candidate["issue"])


def main() -> int:
    try:
        candidates = audit()
    except AuditError as exc:
        print(f"Indeterminate audit: {exc}", file=sys.stderr)
        return 1
    print("# Open issues with only merged visible direct PR references\n")
    print("Evidence for maintainer inspection only; this does not establish issue completion.")
    print(
        "GitHub timeline relationships can change during the audit. Review the current issue and PRs before acting.\n"
    )
    if not candidates:
        print("No candidates were returned by the completed audit.")
    for candidate in candidates:
        print(f"- [#{candidate['issue']}](https://github.com/{REPO}/issues/{candidate['issue']})")
        for pr in candidate["pull_requests"]:
            relation = "GitHub marks closing" if pr["closing"] else "GitHub does not mark closing"
            print(
                f"  - [#{pr['number']}](https://github.com/{REPO}/pull/{pr['number']}): "
                f"{pr['state']}, merged {pr['mergedAt']}, {relation}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
