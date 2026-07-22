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
