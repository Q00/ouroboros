"""Tests for the deep-tier citation liveness audit (grounded-lateral RFC D4).

All tests inject a fake fetcher — the unit suite never touches the network.
"""

from __future__ import annotations

from email.message import Message
import json
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from ouroboros.mcp.tools.citation_check import (
    INVALID,
    UNCHECKED,
    UNREACHABLE,
    VERIFIED,
    _BoundedRedirectHandler,
    _default_fetch,
    audit_citations,
    extract_cited_urls,
)


@pytest.mark.parametrize("same_url", [True, False])
def test_redirect_cap_counts_all_hops_including_repeated_urls(same_url: bool) -> None:
    handler = _BoundedRedirectHandler()
    request = Request("https://example.test/0", method="HEAD")
    headers = Message()
    for index in range(3):
        url = "https://example.test/0" if same_url else f"https://example.test/{index + 1}"
        request = handler.redirect_request(request, None, 302, "Found", headers, url)
        assert request is not None
    with pytest.raises(HTTPError, match="redirect limit exceeded"):
        handler.redirect_request(request, None, 302, "Found", headers, "https://example.test/4")


def test_redirect_limit_http_error_is_not_verified() -> None:
    error = HTTPError("https://example.test/3", 302, "redirect limit exceeded", {}, None)
    with (
        patch("urllib.request.OpenerDirector.open", side_effect=error),
        patch.object(error, "close", wraps=error.close) as close,
    ):
        assert _default_fetch("https://example.test/0", 1.0) is False
    close.assert_called_once()


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_never_reads_response_body(status: int) -> None:
    handler = _BoundedRedirectHandler()
    handler.parent = MagicMock()
    request = Request("https://example.test/0", method="HEAD")
    request.timeout = 1.0
    headers = Message()
    headers["Location"] = "https://example.test/1"
    response = MagicMock()
    response.read.side_effect = AssertionError("redirect body must not be read")

    getattr(handler, f"http_error_{status}")(request, response, status, "redirect", headers)

    response.read.assert_not_called()
    response.close.assert_called_once()
    handler.parent.open.assert_called_once()


def test_redirect_limit_closes_unread_response() -> None:
    handler = _BoundedRedirectHandler()
    request = Request("https://example.test/3", method="HEAD")
    request._citation_redirects = 3
    headers = Message()
    headers["Location"] = "https://example.test/4"
    response = MagicMock()
    with pytest.raises(HTTPError) as error:
        handler.http_error_302(request, response, 302, "redirect", headers)
    error.value.close()
    response.read.assert_not_called()
    response.close.assert_called_once()


@pytest.mark.parametrize("status", [405, 501])
def test_rejected_head_is_closed_before_ranged_get(status: int) -> None:
    error = HTTPError("https://example.test/0", status, "HEAD rejected", {}, None)
    response = MagicMock()
    with (
        patch("urllib.request.OpenerDirector.open", side_effect=[error, response]) as open_request,
        patch.object(error, "close", wraps=error.close) as close,
    ):
        assert _default_fetch("https://example.test/0", 1.0) is True
    close.assert_called_once()
    assert open_request.call_args_list[1].args[0].get_method() == "GET"
    assert open_request.call_args_list[1].args[0].get_header("Range") == "bytes=0-0"


def test_not_found_is_closed_and_not_verified() -> None:
    error = HTTPError("https://example.test/0", 404, "Not Found", {}, None)
    with (
        patch("urllib.request.OpenerDirector.open", side_effect=error) as open_request,
        patch.object(error, "close", wraps=error.close) as close,
    ):
        assert _default_fetch("https://example.test/0", 1.0) is False
    close.assert_called_once()
    open_request.assert_called_once()


def test_redirect_uses_remaining_timeout() -> None:
    handler = _BoundedRedirectHandler(deadline=10.0)
    request = Request("https://example.test/0", method="HEAD")
    request.timeout = 4.0
    with patch("ouroboros.mcp.tools.citation_check.time.monotonic", return_value=9.5):
        handler.redirect_request(request, None, 302, "Found", Message(), "https://example.test/1")
    assert request.timeout == 0.5


def test_expired_redirect_closes_response_without_following() -> None:
    handler = _BoundedRedirectHandler(deadline=10.0)
    handler.parent = MagicMock()
    request = Request("https://example.test/0", method="HEAD")
    request.timeout = 4.0
    headers = Message()
    headers["Location"] = "https://example.test/1"
    response = MagicMock()
    with (
        patch("ouroboros.mcp.tools.citation_check.time.monotonic", return_value=10.0),
        pytest.raises(HTTPError) as error,
    ):
        handler.http_error_302(request, response, 302, "Found", headers)
    error.value.close()
    response.close.assert_called_once()
    handler.parent.open.assert_not_called()


def test_head_fallback_uses_remaining_timeout() -> None:
    error = HTTPError("https://example.test/0", 405, "HEAD rejected", {}, None)
    response = MagicMock()
    with (
        patch("ouroboros.mcp.tools.citation_check.time.monotonic", side_effect=[0, 0, 0.75, 0.9]),
        patch("urllib.request.OpenerDirector.open", side_effect=[error, response]) as open_request,
    ):
        assert _default_fetch("https://example.test/0", 1.0) is True
    assert open_request.call_args_list[1].kwargs["timeout"] == 0.25


def test_head_fallback_is_not_started_after_timeout() -> None:
    error = HTTPError("https://example.test/0", 405, "HEAD rejected", {}, None)
    with (
        patch("ouroboros.mcp.tools.citation_check.time.monotonic", side_effect=[0, 0, 1]),
        patch("urllib.request.OpenerDirector.open", side_effect=error) as open_request,
    ):
        assert _default_fetch("https://example.test/0", 1.0) is False
    open_request.assert_called_once()


def test_response_after_timeout_is_not_verified() -> None:
    response = MagicMock()
    with (
        patch("ouroboros.mcp.tools.citation_check.time.monotonic", side_effect=[0, 0, 1.1]),
        patch("urllib.request.OpenerDirector.open", return_value=response),
    ):
        assert _default_fetch("https://example.test/0", 1.0) is False


_EVIDENCE = (
    "My analysis...\n"
    "```json\n"
    '{"external_sources": ["https://a.example/doc", "https://b.example/spec"],\n'
    ' "claims": [{"claim": "X supports Y", "source": "https://a.example/doc"},\n'
    '            {"claim": "Z is default", "source": "https://c.example/faq"}]}\n'
    "```\n"
)


@pytest.mark.parametrize(
    "invalid",
    [
        "https://bad\ufffd.example/doc",
        "https://ok.example/\ue000",
        "https://ok.example/#\ue000",
        "https://ok.example/\U0001fffe",
        "https://[fe80::1%raw%25eth0]/doc",
    ],
)
def test_excluded_iri_character_preserves_budget_for_later_url(invalid: str) -> None:
    valid = "https://ok.example/doc"
    fetched: list[str] = []

    def fetch(url: str, _timeout: float) -> bool:
        fetched.append(url)
        return True

    evidence = "```json\n" + json.dumps({"external_sources": [invalid, valid]}) + "\n```"
    result = audit_citations([evidence], fetch=fetch, max_urls=1)

    assert result is not None
    assert result["urls"] == {invalid: INVALID, valid: VERIFIED}
    assert fetched == [valid]


@pytest.mark.parametrize(
    "url",
    [
        "https://[V1.fe80]/doc",
        "https://ok.example/?q=\ue000",
        "https://ok.example/\u0378",
        "https://ok.example/\U00010000",
    ],
)
def test_valid_iri_components_reach_fetch_unchanged(url: str) -> None:
    fetched: list[str] = []

    def fetch(candidate: str, _timeout: float) -> bool:
        fetched.append(candidate)
        return True

    evidence = "```json\n" + json.dumps({"external_sources": [url]}) + "\n```"
    result = audit_citations([evidence], fetch=fetch)

    assert result is not None
    assert result["urls"] == {url: VERIFIED}
    assert fetched == [url]


def test_extracts_urls_from_evidence_block_deduplicated_in_order() -> None:
    assert extract_cited_urls(_EVIDENCE) == (
        "https://a.example/doc",
        "https://b.example/spec",
        "https://c.example/faq",
    )


def test_malformed_block_and_plain_text_yield_nothing() -> None:
    assert extract_cited_urls("no evidence here") == ()
    assert extract_cited_urls("```json\n{not json]\n```") == ()


def test_bare_evidence_json_is_audited_without_fencing() -> None:
    text = json.dumps(
        {
            "external_sources": ["https://example.invalid/doc"],
            "claims": [{"source": "https://example.invalid/doc"}],
        }
    )
    calls: list[str] = []
    audit = audit_citations([text], fetch=lambda url, _timeout: calls.append(url) or False)
    assert audit is not None
    assert audit["urls"] == {"https://example.invalid/doc": UNCHECKED}
    assert audit["unverified_present"] is True
    assert calls == []


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://169.254.169.254/", "http://[::1]/"])
def test_bare_evidence_never_fetches_private_destinations(url: str) -> None:
    calls: list[str] = []
    audit = audit_citations(
        [json.dumps({"external_sources": [url]})],
        fetch=lambda candidate, _timeout: calls.append(candidate) or True,
    )
    assert audit is not None
    assert audit["urls"] == {url: UNCHECKED}
    assert calls == []


def test_duplicate_bare_and_fenced_url_preserves_fenced_fetch() -> None:
    url = "https://example.invalid/doc"
    evidence = json.dumps({"external_sources": [url]})
    calls: list[str] = []
    audit = audit_citations(
        [evidence, f"```json\n{evidence}\n```"],
        fetch=lambda candidate, _timeout: calls.append(candidate) or False,
    )
    assert audit is not None
    assert audit["urls"] == {url: UNREACHABLE}
    assert calls == [url]


def test_bare_duplicates_do_not_reorder_fenced_fetch_budget() -> None:
    public = [f"https://example{i}.invalid/doc" for i in range(8)]
    loopback = "http://127.0.0.1:43210/private"
    bare_only = "http://169.254.169.254/latest"
    fenced = "```json\n" + json.dumps({"external_sources": [*public, loopback]}) + "\n```"
    bare = json.dumps({"external_sources": [loopback, bare_only, public[4]]})
    calls: list[str] = []
    result = audit_citations([bare, fenced], fetch=lambda url, _: calls.append(url) or True)
    assert result is not None
    assert calls == public
    assert result["checked"] == 8
    assert result["urls"][loopback] == UNCHECKED
    assert result["urls"][bare_only] == UNCHECKED
    assert result["urls"][public[4]] == VERIFIED


def test_bare_invalids_and_duplicates_do_not_consume_fenced_budget() -> None:
    urls = ["https://a.example/", "https://b.example/"]
    bare = json.dumps({"external_sources": ["file:///etc/passwd", urls[1], urls[1]]})
    fenced = "```json\n" + json.dumps({"external_sources": urls}) + "\n```"
    calls: list[str] = []
    result = audit_citations(
        [bare, fenced], fetch=lambda url, _: calls.append(url) or True, max_urls=1
    )
    assert result is not None
    assert calls == [urls[0]]
    assert result["urls"][urls[1]] == UNCHECKED
    assert result["urls"]["file:///etc/passwd"] == INVALID


def test_bare_non_evidence_and_prose_wrapped_json_do_not_trigger_fetch() -> None:
    calls: list[str] = []

    def fetch(url: str, _timeout: float) -> bool:
        calls.append(url)
        return True

    assert audit_citations(['{"unrelated":"https://example.invalid"}'], fetch=fetch) is None
    assert (
        audit_citations(['opinion {"external_sources":["https://example.invalid"]}'], fetch=fetch)
        is None
    )
    assert calls == []


def test_no_citations_means_no_audit_and_no_network() -> None:
    calls: list[str] = []

    def fetch(url: str, timeout: float) -> bool:
        calls.append(url)
        return True

    assert audit_citations(["plain opinion output"], fetch=fetch) is None
    assert audit_citations(['```json\n{"external_sources": ["   "]}\n```'], fetch=fetch) is None
    assert calls == []


def test_dead_citation_is_marked_not_raised() -> None:
    def fetch(url: str, timeout: float) -> bool:
        return url != "https://c.example/faq"

    audit = audit_citations([_EVIDENCE], fetch=fetch)
    assert audit is not None
    assert audit["urls"]["https://a.example/doc"] == VERIFIED
    assert audit["urls"]["https://c.example/faq"] == UNREACHABLE
    assert audit["unverified_present"] is True


def test_non_http_url_is_invalid_and_never_fetched() -> None:
    calls: list[str] = []

    def fetch(url: str, timeout: float) -> bool:
        calls.append(url)
        return True

    text = '```json\n{"external_sources": ["file:///etc/passwd", "https://ok.example"]}\n```'
    audit = audit_citations([text], fetch=fetch)
    assert audit is not None
    assert audit["urls"]["file:///etc/passwd"] == INVALID
    assert audit["urls"]["https://ok.example"] == VERIFIED
    assert calls == ["https://ok.example"]


def test_url_count_cap_marks_rest_unchecked() -> None:
    urls = [f"https://site{i}.example/page" for i in range(12)]
    text = "```json\n" + '{"external_sources": ' + str(urls).replace("'", '"') + "}\n```"

    audit = audit_citations([text], fetch=lambda _url, _timeout: True, max_urls=3)
    assert audit is not None
    verdicts = list(audit["urls"].values())
    assert verdicts.count(VERIFIED) == 3
    assert verdicts.count(UNCHECKED) == 9
    assert audit["checked"] == 3


def test_small_remaining_budget_is_not_rounded_up() -> None:
    fetch = MagicMock(return_value=True)
    text = '```json\n{"external_sources": ["https://ok.example"]}\n```'
    with patch("ouroboros.mcp.tools.citation_check.time.monotonic", side_effect=[0, 0.95]):
        audit = audit_citations([text], fetch=fetch, total_budget_seconds=1)
    assert audit is not None
    assert audit["urls"]["https://ok.example"] == VERIFIED
    assert fetch.call_args.args[1] == pytest.approx(0.05)


def test_fetcher_exception_degrades_to_unreachable() -> None:
    def fetch(url: str, timeout: float) -> bool:
        raise RuntimeError("dns exploded")

    audit = audit_citations([_EVIDENCE], fetch=fetch)
    assert audit is not None
    assert set(audit["urls"].values()) == {UNREACHABLE}


def test_malformed_url_is_invalid_and_does_not_raise() -> None:
    audit = audit_citations(
        ['```json\n{"external_sources": ["https://[invalid"]}\n```'],
        fetch=lambda _url, _timeout: True,
    )
    assert audit is not None
    assert audit["urls"]["https://[invalid"] == INVALID


def test_malformed_authorities_are_invalid_without_stopping_later_urls() -> None:
    malformed = (
        "https://example.com:not-a-port",
        "https://example.com:65536",
        "https://example.com:",
        "https://bad host.example/doc",
        "https://bad\nhost.example/doc",
        "https://bad\rhost.example/doc",
        "https://bad\thost.example/doc",
        "https://bad\x80host.example/doc",
        "https://%zz.example/doc",
        "https://ok.example/%zz",
    )
    valid = "https://ok.example/doc"
    calls: list[str] = []

    def fetch(url: str, _timeout: float) -> bool:
        calls.append(url)
        return True

    text = "```json\n" + json.dumps({"external_sources": [*malformed, valid]}) + "\n```"
    audit = audit_citations([text], fetch=fetch)

    assert audit is not None
    assert all(audit["urls"][url] == INVALID for url in malformed)
    assert audit["urls"][valid] == VERIFIED
    assert audit["checked"] == 1
    assert calls == [valid]


def test_malformed_urls_do_not_consume_the_valid_url_limit() -> None:
    malformed = [f"https://%z{index}.example/doc" for index in range(8)]
    valid = "https://ok.example/a%20b?q=ok"
    calls: list[str] = []

    def fetch(url: str, _timeout: float) -> bool:
        calls.append(url)
        return True

    text = "```json\n" + json.dumps({"external_sources": [*malformed, valid]}) + "\n```"
    audit = audit_citations([text], fetch=fetch, max_urls=8)

    assert audit is not None
    assert all(audit["urls"][url] == INVALID for url in malformed)
    assert audit["urls"][valid] == VERIFIED
    assert audit["checked"] == 1
    assert calls == [valid]


def test_internationalized_ipvfuture_and_encoded_zone_urls_remain_auditable() -> None:
    urls = (
        "https://bücher.example/über",
        "https://[v1.fe80]/doc",
        "https://[fe80::1%25eth0]/doc",
        "https://[fe80::1%25eth%2D0]/doc",
    )
    calls: list[str] = []

    def fetch(candidate: str, _timeout: float) -> bool:
        calls.append(candidate)
        return True

    text = "```json\n" + json.dumps({"external_sources": urls}, ensure_ascii=False) + "\n```"
    audit = audit_citations([text], fetch=fetch)

    assert audit is not None
    assert all(audit["urls"][url] == VERIFIED for url in urls)
    assert calls == list(urls)


def test_raw_malformed_citations_are_rejected_before_the_valid_url_budget() -> None:
    malformed = (
        " https://bad.example/doc ",
        "https://[fe80::1%eth0]/doc",
        "https://[fe80::1%2Feth0]/doc",
        "https://[fe80::1%25]/doc",
    )
    valid = "https://ok.example/doc"
    calls: list[str] = []

    def fetch(url: str, _timeout: float) -> bool:
        calls.append(url)
        return True

    text = "```json\n" + json.dumps({"external_sources": [*malformed, valid]}) + "\n```"
    audit = audit_citations([text], fetch=fetch, max_urls=1)

    assert audit is not None
    assert all(audit["urls"][url] == INVALID for url in malformed)
    assert audit["urls"][valid] == VERIFIED
    assert audit["checked"] == 1
    assert calls == [valid]


def test_urllib_representable_encoded_zones_are_fetchable() -> None:
    for zone, normalized in (("eth%2D0", "eth-0"), ("eth%3D0", "eth=0")):
        url = f"https://[fe80::1%25{zone}]/doc"
        text = "```json\n" + json.dumps({"external_sources": [url]}) + "\n```"

        with patch("urllib.request.OpenerDirector.open") as urlopen:
            audit = audit_citations([text])

        assert audit is not None
        assert audit["urls"][url] == VERIFIED
        urlopen.assert_called_once()
        assert urlopen.call_args.args[0].full_url == f"https://[fe80::1%25{normalized}]/doc"


def test_bare_evidence_with_trailing_prose_is_unchecked_without_fetch() -> None:
    text = '{"external_sources":["http://127.0.0.1/private"]}\nDone.'
    calls: list[str] = []
    result = audit_citations([text], fetch=lambda url, _: calls.append(url) or True)
    assert result is not None
    assert result["urls"]["http://127.0.0.1/private"] == UNCHECKED
    assert result["unverified_present"] is True
    assert calls == []


def test_trailing_prose_bare_duplicate_does_not_reorder_fenced_budget() -> None:
    urls = [f"https://example.test/{i}" for i in range(8)]
    extra = "http://127.0.0.1/private"
    bare = json.dumps({"external_sources": [extra]}) + "\nDone."
    fenced = "```json\n" + json.dumps({"external_sources": [*urls, extra]}) + "\n```"
    calls: list[str] = []
    result = audit_citations([bare, fenced], fetch=lambda url, _: calls.append(url) or True)
    assert result is not None
    assert calls == urls
    assert result["urls"][extra] == UNCHECKED


def test_bare_prefix_with_brace_inside_string_and_trailing_prose() -> None:
    url = "https://example.test/path"
    text = json.dumps({"claims": [{"text": "brace } in text", "source": url}]}) + "\nDone."
    calls: list[str] = []
    result = audit_citations([text], fetch=lambda url, _: calls.append(url) or True)
    assert result is not None
    assert result["urls"][url] == UNCHECKED
    assert calls == []


def test_invalid_bare_prefix_does_not_scan_later_prose_json() -> None:
    text = "{broken}\n" + json.dumps({"external_sources": ["http://127.0.0.1/private"]})
    calls: list[str] = []
    assert audit_citations([text], fetch=lambda url, _: calls.append(url) or True) is None
    assert calls == []
