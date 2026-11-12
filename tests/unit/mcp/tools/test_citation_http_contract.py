"""Real urllib behavior against a loopback-only HTTP peer."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest

from ouroboros.mcp.tools.citation_check import _default_fetch


@pytest.fixture
def citation_peer():
    seen = []

    class Peer(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_HEAD(self):
            self.respond()

        def do_GET(self):
            self.respond()

        def respond(self):
            seen.append((self.command, self.path, self.headers.get("Range")))
            mode, count = self.path.strip("/").split("/")
            count = int(count)
            if mode == "status":
                self.send_response(count)
            elif mode == "fallback" and self.command == "HEAD":
                self.send_response(405)
            else:
                self.send_response(302 if count else 200)
                if count:
                    self.send_header("Location", f"/{mode}/{count - 1}")
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Peer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("mode", ["head", "fallback"])
@pytest.mark.parametrize("hops", [0, 3, 4])
def test_real_urllib_redirect_cap(citation_peer, mode: str, hops: int) -> None:
    origin, seen = citation_peer
    verified = _default_fetch(f"{origin}/{mode}/{hops}", 2.0)
    print(
        json.dumps({"case": "loopback-citation", "mode": mode, "hops": hops, "verified": verified})
    )
    assert verified is (hops <= 3)
    if mode == "fallback":
        gets = [request for request in seen if request[0] == "GET"]
        assert gets and all(request[2] == "bytes=0-0" for request in gets)


@pytest.mark.parametrize("status", [300, 304, 302])
def test_real_urllib_3xx_without_location_is_unreachable(citation_peer, status: int) -> None:
    origin, _seen = citation_peer
    verified = _default_fetch(f"{origin}/status/{status}", 2.0)
    print(json.dumps({"case": "loopback-citation-status", "status": status, "verified": verified}))
    assert verified is False
