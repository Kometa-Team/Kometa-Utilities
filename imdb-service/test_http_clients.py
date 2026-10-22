"""Tests for lifespan-managed IMDb HTTP clients."""

from unittest.mock import AsyncMock, MagicMock, patch

import http_clients
import pytest


@pytest.mark.asyncio
async def test_graphql_client_is_reused_and_closed():
    client = MagicMock()
    client.aclose = AsyncMock()

    with patch("http_clients.httpx.AsyncClient", return_value=client) as constructor:
        assert http_clients.get_graphql_client() is client
        assert http_clients.get_graphql_client() is client
        constructor.assert_called_once()
        await http_clients.close_clients()

    client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_web_clients_are_separate_per_proxy():
    direct = MagicMock(aclose=AsyncMock())
    first_proxy = MagicMock(aclose=AsyncMock())
    second_proxy = MagicMock(aclose=AsyncMock())

    with patch(
        "http_clients.httpx.AsyncClient",
        side_effect=[direct, first_proxy, second_proxy],
    ):
        assert http_clients.get_web_client() is direct
        assert http_clients.get_web_client() is direct
        assert http_clients.get_web_client("http://proxy-a") is first_proxy
        assert http_clients.get_web_client("http://proxy-a") is first_proxy
        assert http_clients.get_web_client("http://proxy-b") is second_proxy
        await http_clients.close_clients()

    direct.aclose.assert_awaited_once()
    first_proxy.aclose.assert_awaited_once()
    second_proxy.aclose.assert_awaited_once()


@pytest.fixture
def graphql_proxies(monkeypatch):
    """Enable GraphQL proxying with two proxies and a clean cooldown table."""
    monkeypatch.setattr(http_clients, "GRAPHQL_PROXY_ENABLED", True)
    monkeypatch.setattr(http_clients, "GRAPHQL_PROXY_URLS", ["http://p1", "http://p2"])
    monkeypatch.setattr(http_clients, "GRAPHQL_PROXY_ATTEMPTS", 3)
    monkeypatch.setattr(http_clients, "_graphql_proxy_cooldown", {})
    return ["http://p1", "http://p2"]


def _response(status):
    return http_clients.httpx.Response(status, request=http_clients.httpx.Request("POST", "u"))


def _fake_async_clients(monkeypatch, behaviour):
    """Patch _get_client so each proxy key returns a client whose post follows behaviour[key]."""
    calls = []

    def fake_get_client(kind, proxy_url=None):
        assert kind == "graphql"

        async def post(url, **kwargs):
            calls.append(proxy_url)
            outcome = behaviour[proxy_url]
            if isinstance(outcome, Exception):
                raise outcome
            return _response(outcome)

        return MagicMock(post=post)

    monkeypatch.setattr(http_clients, "_get_client", fake_get_client)
    return calls


def test_graphql_client_is_proxied_only_when_enabled(monkeypatch, graphql_proxies):
    assert isinstance(http_clients.get_graphql_client(), http_clients.ProxiedGraphQLClient)
    monkeypatch.setattr(http_clients, "GRAPHQL_PROXY_URLS", [])
    assert not http_clients.graphql_proxy_enabled()


@pytest.mark.asyncio
async def test_proxied_graphql_retries_refused_proxy_on_another(monkeypatch, graphql_proxies):
    calls = _fake_async_clients(monkeypatch, {"http://p1": 403, "http://p2": 200})
    monkeypatch.setattr(http_clients.secrets, "choice", lambda seq: seq[0])

    response = await http_clients.ProxiedGraphQLClient().post("u", json={})

    assert response.status_code == 200
    assert calls == ["http://p1", "http://p2"]
    # The refusing proxy is benched; the next request goes straight to p2.
    assert "http://p1" in http_clients._graphql_proxy_cooldown
    calls.clear()
    await http_clients.ProxiedGraphQLClient().post("u", json={})
    assert calls == ["http://p2"]


@pytest.mark.asyncio
async def test_proxied_graphql_returns_non_retryable_status_as_is(monkeypatch, graphql_proxies):
    calls = _fake_async_clients(monkeypatch, {"http://p1": 400, "http://p2": 400})

    response = await http_clients.ProxiedGraphQLClient().post("u", json={})

    assert response.status_code == 400
    assert len(calls) == 1
    assert http_clients._graphql_proxy_cooldown == {}


@pytest.mark.asyncio
async def test_proxied_graphql_returns_last_refusal_when_all_proxies_refuse(
    monkeypatch, graphql_proxies
):
    calls = _fake_async_clients(monkeypatch, {"http://p1": 403, "http://p2": 429})

    response = await http_clients.ProxiedGraphQLClient().post("u", json={})

    assert response.status_code in (403, 429)
    assert sorted(calls) == ["http://p1", "http://p2"]


@pytest.mark.asyncio
async def test_proxied_graphql_raises_when_every_proxy_errors(monkeypatch, graphql_proxies):
    error = http_clients.httpx.ConnectError("down")
    _fake_async_clients(monkeypatch, {"http://p1": error, "http://p2": error})

    with pytest.raises(http_clients.httpx.ConnectError):
        await http_clients.ProxiedGraphQLClient().post("u", json={})


@pytest.mark.asyncio
async def test_proxied_graphql_goes_direct_when_all_proxies_cooling(monkeypatch, graphql_proxies):
    calls = _fake_async_clients(monkeypatch, {None: 200})
    for proxy in graphql_proxies:
        http_clients._mark_graphql_proxy_failed(proxy)

    response = await http_clients.ProxiedGraphQLClient().post("u", json={})

    assert response.status_code == 200
    assert calls == [None]


def test_proxied_graphql_sync_retries_and_closes(monkeypatch, graphql_proxies):
    monkeypatch.setattr(http_clients.secrets, "choice", lambda seq: seq[0])
    made = {}

    def fake_client(timeout, headers, proxy):
        client = MagicMock()
        if proxy == "http://p1":
            client.post.side_effect = http_clients.httpx.ProxyError("407")
        else:
            client.post.return_value = _response(200)
        made[proxy] = client
        return client

    monkeypatch.setattr(http_clients.httpx, "Client", fake_client)
    sync_client = http_clients.ProxiedGraphQLSyncClient()

    assert sync_client.post("u", json={}).status_code == 200
    assert set(made) == {"http://p1", "http://p2"}
    sync_client.close()
    for client in made.values():
        client.close.assert_called_once()


def test_chart_client_uses_proxied_client_when_enabled(monkeypatch, graphql_proxies):
    import charts

    assert isinstance(charts._new_graphql_client(), http_clients.ProxiedGraphQLSyncClient)
    monkeypatch.setattr(http_clients, "GRAPHQL_PROXY_ENABLED", False)
    with patch("charts.httpx.Client") as constructor:
        charts._new_graphql_client()
    constructor.assert_called_once()
