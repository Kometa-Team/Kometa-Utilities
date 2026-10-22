"""Lifespan-managed HTTP connection pools for IMDb requests."""

import asyncio
import os
import secrets
import time
from typing import Any, Optional
from weakref import WeakKeyDictionary

import httpx

# IMDb's GraphQL edge returns 403 for requests that do not look like a browser
# client.  A browser User-Agent plus x-imdb-client-name is the minimum that gets
# through; a descriptive bot User-Agent is rejected outright.
GRAPHQL_HEADERS = {
    "content-type": "application/json",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "x-imdb-client-name": "imdb-web-next",
    "origin": "https://www.imdb.com",
    "referer": "https://www.imdb.com/",
}

# IMDb answers GraphQL from this server's IP with 403 (seen 2026-10-22) while
# the same request through a residential proxy succeeds, so GraphQL can be
# routed through the parental proxy pool. Off by default; when on, each request
# goes out through a random healthy proxy and is retried on another one.
GRAPHQL_PROXY_ENABLED = os.getenv("IMDB_GRAPHQL_PROXY_ENABLED", "false").lower() == "true"
GRAPHQL_PROXY_URLS = [
    p.strip() for p in os.getenv("PARENTAL_PROXY_URLS", "").split(",") if p.strip()
]
GRAPHQL_PROXY_ATTEMPTS = max(1, int(os.getenv("IMDB_GRAPHQL_PROXY_ATTEMPTS", "3")))
GRAPHQL_PROXY_COOLDOWN_SECONDS = int(os.getenv("IMDB_GRAPHQL_PROXY_COOLDOWN_SECONDS", "300"))
# Statuses that say "this exit IP is refused or struggling", not "bad query".
_GRAPHQL_PROXY_RETRY_STATUSES = {403, 407, 429}

# proxy URL -> time.monotonic() until which it is skipped
_graphql_proxy_cooldown: dict[str, float] = {}


def graphql_proxy_enabled() -> bool:
    """Whether GraphQL requests should go through the proxy pool."""
    return GRAPHQL_PROXY_ENABLED and bool(GRAPHQL_PROXY_URLS)


def _choose_graphql_proxy(exclude: set[str]) -> Optional[str]:
    now = time.monotonic()
    candidates = [
        proxy
        for proxy in GRAPHQL_PROXY_URLS
        if proxy not in exclude and _graphql_proxy_cooldown.get(proxy, 0.0) <= now
    ]
    return secrets.choice(candidates) if candidates else None


def _mark_graphql_proxy_failed(proxy: str) -> None:
    _graphql_proxy_cooldown[proxy] = time.monotonic() + GRAPHQL_PROXY_COOLDOWN_SECONDS


def _should_retry_graphql(response: httpx.Response) -> bool:
    return response.status_code in _GRAPHQL_PROXY_RETRY_STATUSES or response.status_code >= 500


class ProxiedGraphQLClient:
    """Async stand-in for the GraphQL client that sends each post through a proxy.

    Only ``post`` is provided, which is all GraphQL callers use. If every proxy
    is cooling down it falls back to a direct request rather than failing.
    """

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        tried: set[str] = set()
        response: Optional[httpx.Response] = None
        error: Optional[httpx.TransportError] = None
        for _ in range(GRAPHQL_PROXY_ATTEMPTS):
            proxy = _choose_graphql_proxy(tried)
            if proxy is None:
                break
            tried.add(proxy)
            try:
                response = await _get_client("graphql", proxy).post(url, **kwargs)
            except httpx.TransportError as e:
                error = e
                _mark_graphql_proxy_failed(proxy)
                continue
            if not _should_retry_graphql(response):
                return response
            _mark_graphql_proxy_failed(proxy)
        if response is not None:
            return response
        if error is not None:
            raise error
        return await _get_client("graphql").post(url, **kwargs)


class ProxiedGraphQLSyncClient:
    """Blocking counterpart of ProxiedGraphQLClient for chart refresh threads."""

    def __init__(self, timeout: float = 30.0) -> None:
        self._timeout = timeout
        self._clients: dict[Optional[str], httpx.Client] = {}

    def _client(self, proxy: Optional[str]) -> httpx.Client:
        client = self._clients.get(proxy)
        if client is None:
            client = httpx.Client(timeout=self._timeout, headers=GRAPHQL_HEADERS, proxy=proxy)
            self._clients[proxy] = client
        return client

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        tried: set[str] = set()
        response: Optional[httpx.Response] = None
        error: Optional[httpx.TransportError] = None
        for _ in range(GRAPHQL_PROXY_ATTEMPTS):
            proxy = _choose_graphql_proxy(tried)
            if proxy is None:
                break
            tried.add(proxy)
            try:
                response = self._client(proxy).post(url, **kwargs)
            except httpx.TransportError as e:
                error = e
                _mark_graphql_proxy_failed(proxy)
                continue
            if not _should_retry_graphql(response):
                return response
            _mark_graphql_proxy_failed(proxy)
        if response is not None:
            return response
        if error is not None:
            raise error
        return self._client(None).post(url, **kwargs)

    def close(self) -> None:
        for client in self._clients.values():
            client.close()
        self._clients.clear()


_pools: WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[tuple[str, Optional[str]], httpx.AsyncClient]
] = WeakKeyDictionary()


def get_graphql_client() -> Any:
    """Return the IMDb GraphQL client for the current event loop.

    The shared direct client, or a ProxiedGraphQLClient when GraphQL proxying
    is enabled. Either way, callers use ``await client.post(...)``.
    """
    if graphql_proxy_enabled():
        return ProxiedGraphQLClient()
    return _get_client("graphql")


def get_web_client(proxy_url: Optional[str] = None) -> httpx.AsyncClient:
    """Return a direct or proxy-specific IMDb web client."""
    return _get_client("web", proxy_url)


def _get_client(kind: str, proxy_url: Optional[str] = None) -> httpx.AsyncClient:
    loop = asyncio.get_running_loop()
    pool = _pools.setdefault(loop, {})
    key = (kind, proxy_url)
    client = pool.get(key)
    if client is not None:
        return client

    if kind == "graphql":
        client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=30.0,
            headers=GRAPHQL_HEADERS,
            proxy=proxy_url,
            limits=httpx.Limits(
                max_connections=4 if proxy_url else 20,
                max_keepalive_connections=2 if proxy_url else 10,
                keepalive_expiry=30.0,
            ),
        )
    else:
        client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=30.0,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; Kometa-Utilities/IMDb-Service)",
                "Accept-Language": "en-US,en;q=0.9",
            },
            proxy=proxy_url,
            limits=httpx.Limits(
                max_connections=2 if proxy_url else 20,
                max_keepalive_connections=1 if proxy_url else 10,
                keepalive_expiry=30.0,
            ),
        )
    pool[key] = client
    return client


async def close_clients() -> None:
    """Close all clients owned by the current event loop."""
    loop = asyncio.get_running_loop()
    pool = _pools.pop(loop, {})
    if pool:
        await asyncio.gather(*(client.aclose() for client in pool.values()))
