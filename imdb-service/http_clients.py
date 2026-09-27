"""Lifespan-managed HTTP connection pools for IMDb requests."""

import asyncio
from typing import Optional
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

_pools: WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[tuple[str, Optional[str]], httpx.AsyncClient]
] = WeakKeyDictionary()


def get_graphql_client() -> httpx.AsyncClient:
    """Return the shared IMDb GraphQL client for the current event loop."""
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
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
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
