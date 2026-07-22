"""GraphQL constraint cache and fetcher for IMDb advanced title search.

Kometa sends resolved constraint values (e.g. company IDs, interest IDs, keyword
slugs) that are not present in the downloaded IMDb datasets.  This module turns
those constraints into IMDb title ID lists via the public GraphQL
``advancedTitleSearch`` endpoint, caches the results in SQLite, and exposes them
for intersection with the local dataset filters.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import aiosqlite
import http_clients
import httpx
import singleflight

GRAPHQL_URL = os.getenv("IMDB_GRAPHQL_URL", "https://api.graphql.imdb.com/")

# Pagination limits for constraint result fetching.
DEFAULT_PAGE_SIZE = 250
DEFAULT_MAX_PAGES = 40  # 10,000 titles per constraint

# TTL per constraint type (days).  Stable things get long TTLs; volatile things
# get short ones.  These can be overridden via the ``ttl_days`` argument.
CONSTRAINT_CACHE_TTL_DAYS: dict[str, int] = {
    "company": 30,
    "event": 30,
    "content_rating": 30,
    "interest": 30,
    "topic": 14,
    "keyword": 7,
    "location": 7,
    "alternate_version": 7,
    "crazy_credit": 7,
    "goof": 7,
    "plot": 7,
    "quote": 7,
    "soundtrack": 7,
    "trivia": 7,
    "popularity": 1,
    "list": 1,
    "character": 7,
    "search": 1,
}


def _canonical_value(value: Any) -> Any:
    """Recursively canonicalize a constraint params value for stable keying."""
    if isinstance(value, dict):
        return {k: _canonical_value(value[k]) for k in sorted(value)}
    if isinstance(value, list):
        return sorted(_canonical_value(item) for item in value)
    return value


def _constraint_cache_key(constraint_type: str, params: dict[str, Any]) -> str:
    """Return a deterministic cache key for a constraint query."""
    canonical = _canonical_value({"type": constraint_type, "params": params})
    return json.dumps(canonical, sort_keys=True, separators=(",", ":"))


def _advanced_title_search_query() -> str:
    """Return the static GraphQL query used for all constraint searches."""
    return (
        "query($constraints: AdvancedTitleSearchConstraints!, $first: Int!, $after: String) {"
        "  advancedTitleSearch(first: $first, after: $after, constraints: $constraints) {"
        "    edges { node { title { id } } }"
        "    pageInfo { hasNextPage endCursor }"
        "  }"
        "}"
    )


async def _ensure_constraint_cache_table(db_path: Path) -> None:
    """Create the constraint cache table if it does not exist."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS imdb_constraint_cache (
                constraint_type TEXT NOT NULL,
                constraint_key TEXT NOT NULL,
                tconsts TEXT NOT NULL,
                expiration_date TEXT NOT NULL,
                PRIMARY KEY (constraint_type, constraint_key)
            )
            """
        )
        await db.commit()


async def load_constraint_cache(
    db_path: Path,
    constraint_type: str,
    params: dict[str, Any],
) -> tuple[Optional[list[str]], bool]:
    """Load a cached constraint result if it exists.

    Returns ``(ids, expired)``.  ``ids`` is ``None`` when no cache entry exists.
    ``expired`` is ``True`` when the entry exists but is past its TTL.
    """
    await _ensure_constraint_cache_table(db_path)
    key = _constraint_cache_key(constraint_type, params)

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await db.execute(
            "SELECT tconsts, expiration_date FROM imdb_constraint_cache "
            "WHERE constraint_type = ? AND constraint_key = ?",
            (constraint_type, key),
        )
        data = await row.fetchone()

    if data is None:
        return None, False

    now = datetime.now(timezone.utc)
    expiration = datetime.fromisoformat(data["expiration_date"])
    ids = json.loads(data["tconsts"])
    return ids, expiration <= now


async def save_constraint_cache(
    db_path: Path,
    constraint_type: str,
    params: dict[str, Any],
    ids: list[str],
    ttl_days: Optional[int] = None,
) -> None:
    """Persist a constraint result with a TTL."""
    await _ensure_constraint_cache_table(db_path)
    key = _constraint_cache_key(constraint_type, params)
    ttl = timedelta(
        days=ttl_days if ttl_days is not None else CONSTRAINT_CACHE_TTL_DAYS.get(constraint_type, 7)
    )
    expiration_date = (datetime.now(timezone.utc) + ttl).isoformat()

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO imdb_constraint_cache(constraint_type, constraint_key, tconsts, expiration_date)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(constraint_type, constraint_key) DO UPDATE SET
                tconsts = excluded.tconsts,
                expiration_date = excluded.expiration_date
            """,
            (constraint_type, key, json.dumps(ids), expiration_date),
        )
        await db.commit()


async def fetch_constraint_ids(
    params: dict[str, Any],
    *,
    graphql_url: str = GRAPHQL_URL,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> list[str]:
    """Fetch all matching title IDs from IMDb GraphQL, paginating as needed.

    ``params`` is a raw ``AdvancedTitleSearchConstraints`` input object, e.g.
    ``{"keywordConstraint": {"anyKeywords": ["prison"]}}``.
    """
    query = _advanced_title_search_query()
    ids: list[str] = []
    cursor: Optional[str] = None
    page = 0

    client = http_clients.get_graphql_client()
    while page < max_pages:
        variables: dict[str, Any] = {
            "constraints": params,
            "first": page_size,
        }
        if cursor:
            variables["after"] = cursor

        response = await client.post(graphql_url, json={"query": query, "variables": variables})
        response.raise_for_status()
        data = response.json()

        if "errors" in data:
            error_message = data["errors"][0].get("message", "unknown error")
            raise RuntimeError(f"IMDb GraphQL constraint search failed: {error_message}")

        search_data = (data.get("data") or {}).get("advancedTitleSearch") or {}
        edges = search_data.get("edges", [])
        for edge in edges:
            title_id = ((edge.get("node") or {}).get("title") or {}).get("id")
            if title_id:
                ids.append(title_id)

        page_info = search_data.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        if not cursor:
            break
        page += 1

    return ids


def _advanced_search_query() -> str:
    """Return the GraphQL query for a full advanced search (constraints + sort)."""
    return (
        "query($constraints: AdvancedTitleSearchConstraints!, $sort: AdvancedTitleSearchSort,"
        " $first: Int!, $after: String) {"
        "  advancedTitleSearch(first: $first, after: $after, constraints: $constraints, sort: $sort) {"
        "    total"
        "    edges { node { title { id } } }"
        "    pageInfo { hasNextPage endCursor }"
        "  }"
        "}"
    )


async def fetch_search_ids(
    search_constraints: dict[str, Any],
    *,
    sort: Optional[dict[str, Any]] = None,
    limit: Optional[int] = None,
    graphql_url: str = GRAPHQL_URL,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> list[str]:
    """Run a single paginated ``advancedTitleSearch`` and return ordered title IDs.

    Unlike :func:`fetch_constraint_ids`, this preserves IMDb's result ordering
    (via ``sort``) and stops once ``limit`` IDs have been collected.  The whole
    query is sent as one combined constraints object so no cross-constraint
    intersection is needed.
    """
    query = _advanced_search_query()
    ids: list[str] = []
    cursor: Optional[str] = None
    page = 0

    client = http_clients.get_graphql_client()
    while page < max_pages:
        remaining = None if limit is None else max(0, limit - len(ids))
        if remaining == 0:
            break
        first = page_size if remaining is None else min(page_size, remaining)
        variables: dict[str, Any] = {"constraints": search_constraints, "first": first}
        if sort:
            variables["sort"] = sort
        if cursor:
            variables["after"] = cursor

        response = await client.post(graphql_url, json={"query": query, "variables": variables})
        response.raise_for_status()
        data = response.json()

        if "errors" in data:
            error_message = data["errors"][0].get("message", "unknown error")
            raise RuntimeError(f"IMDb GraphQL advanced search failed: {error_message}")

        search_data = (data.get("data") or {}).get("advancedTitleSearch") or {}
        for edge in search_data.get("edges", []):
            title_id = ((edge.get("node") or {}).get("title") or {}).get("id")
            if title_id:
                ids.append(title_id)

        if limit is not None and len(ids) >= limit:
            ids = ids[:limit]
            break

        page_info = search_data.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        if not cursor:
            break
        page += 1

    return ids


async def get_search_ids(
    db_path: Path,
    search_constraints: dict[str, Any],
    *,
    sort: Optional[dict[str, Any]] = None,
    limit: Optional[int] = None,
    ignore_cache: bool = False,
    ttl_days: Optional[int] = None,
    graphql_url: str = GRAPHQL_URL,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> tuple[list[str], bool]:
    """Return ordered search IDs, using the cache unless ignored or stale.

    Returns ``(ids, cache_hit)``.
    """
    cache_params = {"constraints": search_constraints, "sort": sort, "limit": limit}
    cached, expired = (
        (None, False)
        if ignore_cache
        else await load_constraint_cache(db_path, "search", cache_params)
    )
    if cached is not None and not expired:
        return cached, True

    async def fetch_and_cache() -> list[str]:
        ids = await fetch_search_ids(
            search_constraints,
            sort=sort,
            limit=limit,
            graphql_url=graphql_url,
            page_size=page_size,
            max_pages=max_pages,
        )
        await save_constraint_cache(
            db_path,
            "search",
            cache_params,
            ids,
            ttl_days=ttl_days if ttl_days is not None else 1,
        )
        return ids

    ids = await singleflight.run_singleflight(
        (
            "constraint-search",
            str(db_path),
            _constraint_cache_key("search", cache_params),
            graphql_url,
            page_size,
            max_pages,
            ttl_days,
        ),
        fetch_and_cache,
    )
    return ids, False


async def get_constraint_ids(
    db_path: Path,
    constraint_type: str,
    params: dict[str, Any],
    *,
    ignore_cache: bool = False,
    ttl_days: Optional[int] = None,
    graphql_url: str = GRAPHQL_URL,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> list[str]:
    """Return title IDs for a constraint, using the cache unless ignored or stale."""
    cached, expired = (
        (None, False)
        if ignore_cache
        else await load_constraint_cache(db_path, constraint_type, params)
    )
    if cached is not None and not expired:
        return cached

    async def fetch_and_cache() -> list[str]:
        ids = await fetch_constraint_ids(
            params,
            graphql_url=graphql_url,
            page_size=page_size,
            max_pages=max_pages,
        )
        await save_constraint_cache(db_path, constraint_type, params, ids, ttl_days=ttl_days)
        return ids

    return await singleflight.run_singleflight(
        (
            "constraint",
            str(db_path),
            constraint_type,
            _constraint_cache_key(constraint_type, params),
            graphql_url,
            page_size,
            max_pages,
            ttl_days,
        ),
        fetch_and_cache,
    )
