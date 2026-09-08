import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from job_monitor.config import Settings
from job_monitor.sources import PublicTransport, Web, check_public_url


def test_secrets_not_exposed_by_settings_repr():
    settings = Settings(
        **{
            key: "synthetic-secret-marker"
            for key in (
                "database_url",
                "telegram_bot_token",
                "openai_api_key",
                "brave_api_key",
                "notion_api_key",
                "private_config_json",
            )
        }
    )
    assert "synthetic-secret-marker" not in repr(settings)


async def test_dns_rebinding_uses_validated_ip_and_original_tls_name(monkeypatch):
    calls = []

    def dns(*args):
        calls.append(args)
        ip = "93.184.216.34" if len(calls) == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    transport = PublicTransport()
    await transport.inner.aclose()
    captured = []

    async def send(request):
        captured.append(request)
        return httpx.Response(200, content=b"ok")

    transport.inner = httpx.MockTransport(send)
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        response = await client.get("https://jobs.example/jobs/1")
    assert captured[0].url.host == "93.184.216.34"
    assert captured[0].headers["host"] == "jobs.example"
    assert captured[0].extensions["sni_hostname"] == "jobs.example"
    assert response.request.url.host == "jobs.example"
    assert len(calls) == 1


async def test_mixed_private_public_dns_is_rejected(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in ["93.184.216.34", "10.0.0.1"]
        ],
    )
    with pytest.raises(ValueError):
        await check_public_url("https://jobs.example/role")


async def test_redirect_cannot_reach_metadata_or_forward_token(monkeypatch):
    def dns(host, *args):
        ip = "169.254.169.254" if host == "169.254.169.254" else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    web = Web()
    await web.client.aclose()
    calls = []

    def send(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})

    web.client = httpx.AsyncClient(transport=httpx.MockTransport(send))
    try:
        with pytest.raises(ValueError):
            await web.get("https://example.com", headers={"X-Subscription-Token": "synthetic"})
        assert len(calls) == 1
    finally:
        await web.close()


async def test_redirect_drops_credentials(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )
    web = Web()
    await web.client.aclose()
    calls = []

    def send(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(302, headers={"location": "https://other.example/path"})
        assert "X-Subscription-Token" not in request.headers
        assert "private-query" not in str(request.url)
        return httpx.Response(200, text="ok")

    web.client = httpx.AsyncClient(transport=httpx.MockTransport(send))
    try:
        await web.get(
            "https://example.com",
            headers={"X-Subscription-Token": "synthetic"},
            params={"q": "private-query"},
        )
        assert len(calls) == 2
    finally:
        await web.close()


async def test_unauthorized_telegram_command_does_not_read_database():
    from job_monitor.bot import build_bot

    settings = Settings(telegram_bot_token="123:test", telegram_user_id=123, telegram_chat_id=123)

    def forbidden_factory():
        raise AssertionError("Unauthorized database access")

    app = build_bot(forbidden_factory, settings)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=999),
        effective_chat=SimpleNamespace(id=123),
        message=SimpleNamespace(reply_text=AsyncMock()),
    )
    for handler in app.handlers[0]:
        if hasattr(handler, "commands"):
            await handler.callback(update, None)
    update.message.reply_text.assert_not_called()
