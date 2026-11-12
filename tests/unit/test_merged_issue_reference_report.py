"""Tests for the read-only issue/PR reference report."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "merged_issue_reference_report",
    Path(__file__).parents[2] / "scripts/report-merged-issue-references.py",
)
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


def page(nodes, cursor=None):
    return {"nodes": nodes, "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor}}


def event(number, state="MERGED", closing=False, repo="Q00/ouroboros", draft=False):
    return {
        "willCloseTarget": closing,
        "source": {
            "__typename": "PullRequest",
            "number": number,
            "state": state,
            "mergedAt": "2026-01-01T00:00:00Z" if state == "MERGED" else None,
            "isDraft": draft,
            "repository": {"nameWithOwner": repo},
        },
    }


def test_paginated_candidates_only_and_relationships():
    def read(query, variables):
        cursor = variables["cursor"]
        if query == report.ISSUES:
            return {
                "repository": {
                    "issues": page([{"number": 1}, {"number": 2}], "more")
                    if cursor is None
                    else page([{"number": 3}, {"number": 4}])
                }
            }
        number = variables["number"]
        events = {
            (1, None): page(
                [event(10), {"willCloseTarget": False, "source": {"__typename": "Issue"}}], "next"
            ),
            (1, "next"): page([event(10, closing=True), event(11)]),
            (2, None): page([event(12), event(13, state="OPEN", draft=True)]),
            (3, None): page([event(14, state="CLOSED")]),
            (4, None): page([event(15, repo="other/repo")]),
        }
        return {"repository": {"issue": {"state": "OPEN", "timelineItems": events[number, cursor]}}}

    expected = [
        {
            "issue": 1,
            "pull_requests": [
                {
                    "number": 10,
                    "state": "MERGED",
                    "mergedAt": "2026-01-01T00:00:00Z",
                    "closing": True,
                },
                {
                    "number": 11,
                    "state": "MERGED",
                    "mergedAt": "2026-01-01T00:00:00Z",
                    "closing": False,
                },
            ],
        }
    ]
    assert report.audit(read) == expected
    assert report.audit(read) == expected


@pytest.mark.parametrize("source", [None, {"__typename": "PullRequest", "repository": None}, {}])
def test_missing_reference_is_indeterminate(source):
    def read(query, variables):
        if query == report.ISSUES:
            return {"repository": {"issues": page([{"number": 1}])}}
        return {
            "repository": {
                "issue": {
                    "state": "OPEN",
                    "timelineItems": page(
                        [
                            {"willCloseTarget": False, "source": source},
                        ]
                    ),
                }
            }
        }

    with pytest.raises(report.AuditError):
        report.audit(read)


def test_repeated_cursor_is_indeterminate():
    with pytest.raises(report.AuditError):
        report.pages(lambda _cursor: page([], "same"))


def test_api_retry_then_success(monkeypatch):
    calls = []

    def run(*args, **kwargs):
        calls.append(1)
        if len(calls) < 3:
            raise report.subprocess.TimeoutExpired("gh", 60)
        return report.subprocess.CompletedProcess([], 0, '{"data": {"ok": true}}', "")

    monkeypatch.setattr(report.subprocess, "run", run)
    monkeypatch.setattr(report.time, "sleep", lambda _: None)
    assert report.graphql("query", {}) == {"ok": True}
    assert len(calls) == 3


def test_failure_does_not_print_empty_report(monkeypatch, capsys):
    def fail():
        raise report.AuditError("unavailable")

    monkeypatch.setattr(report, "audit", fail)
    assert report.main() == 1
    output = capsys.readouterr()
    assert not output.out
    assert "Indeterminate audit" in output.err
