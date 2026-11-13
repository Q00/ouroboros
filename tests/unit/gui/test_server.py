"""The console API refuses calls without its token or from a non-loopback Host."""

from __future__ import annotations

import http.client
import json

import pytest

from ouroboros.gui.server import serve_background


@pytest.fixture
def console():
    server, _thread = serve_background(port=0)
    yield server
    server.shutdown()
    server.server_close()


def _request(
    server,
    method: str,
    path: str,
    *,
    token: bool = True,
    host: str | None = None,
    body=None,
    origin=None,
):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    headers = {}
    if token:
        headers["X-Ouroboros-Token"] = server.token
    if host is not None:
        headers["Host"] = host
    if origin is not None:
        headers["Origin"] = origin
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=data, headers=headers)
    response = conn.getresponse()
    payload = response.read()
    conn.close()
    return response.status, payload


def test_healthz_is_public(console) -> None:
    status, body = _request(console, "GET", "/healthz", token=False)
    assert status == 200
    assert json.loads(body)["service"] == "ouroboros-gui"


def test_api_requires_token(console) -> None:
    status, body = _request(console, "GET", "/api/jobs", token=False)
    assert status == 401
    assert json.loads(body)["code"] == "unauthorized"


def test_api_accepts_token(console) -> None:
    status, body = _request(console, "GET", "/api/jobs")
    assert status == 200
    assert json.loads(body) == {"jobs": []}


def test_dns_rebinding_host_is_refused(console) -> None:
    status, _ = _request(console, "GET", "/api/jobs", host="evil.example:80")
    assert status == 403


def test_cross_origin_page_is_refused_even_with_token(console) -> None:
    status, _ = _request(console, "GET", "/api/jobs", origin="https://evil.example")
    assert status == 401


def test_post_requires_json(console) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", console.server_port, timeout=10)
    conn.request(
        "POST",
        "/api/settings/preview",
        body=b"a=b",
        headers={
            "X-Ouroboros-Token": console.token,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    response = conn.getresponse()
    response.read()
    conn.close()
    assert response.status == 415


def test_index_embeds_token(console) -> None:
    status, body = _request(console, "GET", "/", token=False)
    if status == 503:  # page not built in this checkout
        pytest.skip("console page not built")
    assert status == 200
    assert console.token.encode() in body


def test_unknown_api_route_is_404(console) -> None:
    status, _ = _request(console, "GET", "/api/nope")
    assert status == 404
