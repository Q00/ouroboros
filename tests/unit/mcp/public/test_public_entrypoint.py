import pytest

from ouroboros.mcp.public.app import public_bind_settings, serve_public


def test_public_bind_settings_default_to_network_container_boundary(monkeypatch):
    monkeypatch.delenv("OUROBOROS_PUBLIC_HOST", raising=False)
    monkeypatch.delenv("OUROBOROS_PUBLIC_PORT", raising=False)

    assert public_bind_settings() == ("127.0.0.1", 8080)


def test_public_bind_settings_accept_deployment_environment(monkeypatch):
    monkeypatch.setenv("OUROBOROS_PUBLIC_HOST", "127.0.0.1")
    monkeypatch.setenv("OUROBOROS_PUBLIC_PORT", "9000")

    assert public_bind_settings() == ("127.0.0.1", 9000)


def test_public_bind_settings_reject_invalid_port(monkeypatch):
    monkeypatch.setenv("OUROBOROS_PUBLIC_PORT", "invalid")

    with pytest.raises(ValueError, match="OUROBOROS_PUBLIC_PORT"):
        public_bind_settings()


def test_public_bind_settings_requires_auth_gateway_for_non_loopback(monkeypatch):
    monkeypatch.setenv("OUROBOROS_PUBLIC_HOST", "0.0.0.0")
    monkeypatch.delenv("OUROBOROS_PUBLIC_BEHIND_AUTH_GATEWAY", raising=False)

    with pytest.raises(ValueError, match="AUTH_GATEWAY"):
        public_bind_settings()

    monkeypatch.setenv("OUROBOROS_PUBLIC_BEHIND_AUTH_GATEWAY", "1")
    assert public_bind_settings() == ("0.0.0.0", 8080)


@pytest.mark.asyncio
async def test_serve_public_uses_streamable_http(monkeypatch):
    seen = {}

    class StubServer:
        async def serve(self, transport, host, port):
            seen.update(transport=transport, host=host, port=port)

        async def shutdown(self):
            seen["shutdown"] = True

    monkeypatch.setattr("ouroboros.mcp.public.app.create_public_server", lambda: StubServer())
    monkeypatch.setenv("OUROBOROS_PUBLIC_HOST", "127.0.0.1")
    monkeypatch.setenv("OUROBOROS_PUBLIC_PORT", "9000")

    await serve_public()

    assert seen == {
        "transport": "streamable-http",
        "host": "127.0.0.1",
        "port": 9000,
        "shutdown": True,
    }
