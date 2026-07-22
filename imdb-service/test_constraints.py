"""Tests for the GraphQL constraint cache/fetcher module."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import constraints
import pytest


def _make_graphql_response(ids, has_next_page=False, end_cursor=None):
    """Build a paginated advancedTitleSearch response for mocking."""
    return {
        "data": {
            "advancedTitleSearch": {
                "edges": [{"node": {"title": {"id": tid}}} for tid in ids],
                "pageInfo": {"hasNextPage": has_next_page, "endCursor": end_cursor},
            }
        }
    }


def _mock_async_httpx_client(responses):
    """Return an AsyncMock that mimics httpx.AsyncClient for a list of responses."""
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=responses)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return mock_client


def test_constraint_cache_key_is_stable_and_order_independent():
    """The cache key must be deterministic regardless of dict/list ordering."""
    key1 = constraints._constraint_cache_key(
        "keyword", {"keywordConstraint": {"anyKeywords": ["prison", "escape"]}}
    )
    key2 = constraints._constraint_cache_key(
        "keyword", {"keywordConstraint": {"anyKeywords": ["escape", "prison"]}}
    )
    assert key1 == key2
    assert isinstance(key1, str)
    assert len(key1) > 0


@pytest.mark.asyncio
async def test_fetch_constraint_ids_returns_ids(tmp_path):
    """A single-page GraphQL response is parsed into a list of tconst IDs."""
    response = _make_graphql_response(["tt0111161", "tt0068646"])
    mock_client = _mock_async_httpx_client(
        [MagicMock(json=lambda: response, raise_for_status=MagicMock())]
    )

    with patch("constraints.httpx.AsyncClient", return_value=mock_client):
        ids = await constraints.fetch_constraint_ids(
            {"keywordConstraint": {"anyKeywords": ["prison"]}}
        )

    assert ids == ["tt0111161", "tt0068646"]


@pytest.mark.asyncio
async def test_fetch_constraint_ids_paginates(tmp_path):
    """Pagination cursors are followed until hasNextPage is false."""
    page1 = _make_graphql_response(["tt0111161"], has_next_page=True, end_cursor="cursor1")
    page2 = _make_graphql_response(["tt0068646"], has_next_page=False)
    responses = [
        MagicMock(json=lambda d=data: d, raise_for_status=MagicMock()) for data in [page1, page2]
    ]
    mock_client = _mock_async_httpx_client(responses)

    with patch("constraints.httpx.AsyncClient", return_value=mock_client):
        ids = await constraints.fetch_constraint_ids(
            {"keywordConstraint": {"anyKeywords": ["prison"]}}, page_size=1
        )

    assert ids == ["tt0111161", "tt0068646"]
    assert mock_client.post.call_count == 2


@pytest.mark.asyncio
async def test_fetch_constraint_ids_respects_max_pages(tmp_path):
    """Fetching stops after max_pages even if more pages are advertised."""
    page = _make_graphql_response(["tt0111161"], has_next_page=True, end_cursor="cursor1")
    responses = [MagicMock(json=lambda: page, raise_for_status=MagicMock())] * 5
    mock_client = _mock_async_httpx_client(responses)

    with patch("constraints.httpx.AsyncClient", return_value=mock_client):
        ids = await constraints.fetch_constraint_ids(
            {"keywordConstraint": {"anyKeywords": ["prison"]}},
            page_size=1,
            max_pages=3,
        )

    assert len(ids) == 3
    assert mock_client.post.call_count == 3


@pytest.mark.asyncio
async def test_fetch_constraint_ids_stops_when_cursor_missing(tmp_path):
    """Fetching stops if the API claims more pages but provides no cursor."""
    page = _make_graphql_response(["tt0111161"], has_next_page=True, end_cursor=None)
    mock_client = _mock_async_httpx_client(
        [MagicMock(json=lambda: page, raise_for_status=MagicMock())]
    )

    with patch("constraints.httpx.AsyncClient", return_value=mock_client):
        ids = await constraints.fetch_constraint_ids(
            {"keywordConstraint": {"anyKeywords": ["prison"]}}, page_size=1
        )

    assert ids == ["tt0111161"]
    assert mock_client.post.call_count == 1


@pytest.mark.asyncio
async def test_fetch_constraint_ids_raises_on_graphql_error(tmp_path):
    """A GraphQL error in the response body is surfaced as RuntimeError."""
    response = {"errors": [{"message": "Bad constraint"}]}
    mock_client = _mock_async_httpx_client(
        [MagicMock(json=lambda: response, raise_for_status=MagicMock())]
    )

    with patch("constraints.httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(RuntimeError, match="Bad constraint"):
            await constraints.fetch_constraint_ids({"keywordConstraint": {"bad": True}})


@pytest.mark.asyncio
async def test_save_and_load_constraint_cache_round_trip(tmp_path):
    """Saved constraint IDs can be loaded back while still fresh."""
    db_path = tmp_path / "imdb.db"
    params = {"keywordConstraint": {"anyKeywords": ["prison"]}}

    await constraints.save_constraint_cache(db_path, "keyword", params, ["tt0111161"])
    ids, expired = await constraints.load_constraint_cache(db_path, "keyword", params)

    assert ids == ["tt0111161"]
    assert expired is False


@pytest.mark.asyncio
async def test_load_constraint_cache_reports_expired_entries(tmp_path):
    """Expired cache entries are returned but flagged as expired."""
    db_path = tmp_path / "imdb.db"
    params = {"keywordConstraint": {"anyKeywords": ["prison"]}}
    await constraints._ensure_constraint_cache_table(db_path)
    key = constraints._constraint_cache_key("keyword", params)
    expiration = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

    async with constraints.aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO imdb_constraint_cache(constraint_type, constraint_key, tconsts, expiration_date) "
            "VALUES (?, ?, ?, ?)",
            ("keyword", key, json.dumps(["tt0111161"]), expiration),
        )
        await db.commit()

    ids, expired = await constraints.load_constraint_cache(db_path, "keyword", params)
    assert ids == ["tt0111161"]
    assert expired is True


@pytest.mark.asyncio
async def test_get_constraint_ids_returns_cached_value_when_fresh(tmp_path):
    """Fresh cache entries are returned without calling GraphQL."""
    db_path = tmp_path / "imdb.db"
    params = {"keywordConstraint": {"anyKeywords": ["prison"]}}
    await constraints.save_constraint_cache(db_path, "keyword", params, ["tt0111161"])

    with patch("constraints.httpx.AsyncClient") as mock_client_class:
        ids = await constraints.get_constraint_ids(db_path, "keyword", params)

    assert ids == ["tt0111161"]
    mock_client_class.assert_not_called()


@pytest.mark.asyncio
async def test_get_constraint_ids_returns_cached_empty_value(tmp_path):
    """A fresh empty result is a cache hit rather than a repeated fetch."""
    db_path = tmp_path / "imdb.db"
    params = {"keywordConstraint": {"anyKeywords": ["no-match"]}}
    await constraints.save_constraint_cache(db_path, "keyword", params, [])

    with patch("constraints.fetch_constraint_ids", new_callable=AsyncMock) as fetch:
        ids = await constraints.get_constraint_ids(db_path, "keyword", params)

    assert ids == []
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_constraint_ids_coalesces_concurrent_misses(tmp_path, monkeypatch):
    db_path = tmp_path / "imdb.db"
    params = {"keywordConstraint": {"anyKeywords": ["prison"]}}
    calls = 0

    async def fake_fetch(*args, **kwargs):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return ["tt0111161"]

    monkeypatch.setattr(constraints, "fetch_constraint_ids", fake_fetch)
    first, second = await asyncio.gather(
        constraints.get_constraint_ids(db_path, "keyword", params),
        constraints.get_constraint_ids(db_path, "keyword", params),
    )

    assert first == second == ["tt0111161"]
    assert calls == 1


@pytest.mark.asyncio
async def test_get_constraint_ids_fetches_when_missing(tmp_path):
    """Missing cache entries trigger a GraphQL fetch and are persisted."""
    db_path = tmp_path / "imdb.db"
    params = {"keywordConstraint": {"anyKeywords": ["prison"]}}
    response = _make_graphql_response(["tt0111161"])
    mock_client = _mock_async_httpx_client(
        [MagicMock(json=lambda: response, raise_for_status=MagicMock())]
    )

    with patch("constraints.httpx.AsyncClient", return_value=mock_client):
        ids = await constraints.get_constraint_ids(db_path, "keyword", params)

    assert ids == ["tt0111161"]

    cached, expired = await constraints.load_constraint_cache(db_path, "keyword", params)
    assert cached == ["tt0111161"]
    assert expired is False


@pytest.mark.asyncio
async def test_get_constraint_ids_refreshes_stale_cache(tmp_path):
    """Stale cache entries are refreshed from GraphQL."""
    db_path = tmp_path / "imdb.db"
    params = {"keywordConstraint": {"anyKeywords": ["prison"]}}
    await constraints.save_constraint_cache(db_path, "keyword", params, ["ttOLD"], ttl_days=-1)

    response = _make_graphql_response(["ttNEW"])
    mock_client = _mock_async_httpx_client(
        [MagicMock(json=lambda: response, raise_for_status=MagicMock())]
    )

    with patch("constraints.httpx.AsyncClient", return_value=mock_client):
        ids = await constraints.get_constraint_ids(db_path, "keyword", params)

    assert ids == ["ttNEW"]


@pytest.mark.asyncio
async def test_get_constraint_ids_ignore_cache_forces_refresh(tmp_path):
    """ignore_cache bypasses even a fresh cache entry."""
    db_path = tmp_path / "imdb.db"
    params = {"keywordConstraint": {"anyKeywords": ["prison"]}}
    await constraints.save_constraint_cache(db_path, "keyword", params, ["ttOLD"])

    response = _make_graphql_response(["ttNEW"])
    mock_client = _mock_async_httpx_client(
        [MagicMock(json=lambda: response, raise_for_status=MagicMock())]
    )

    with patch("constraints.httpx.AsyncClient", return_value=mock_client):
        ids = await constraints.get_constraint_ids(db_path, "keyword", params, ignore_cache=True)

    assert ids == ["ttNEW"]


@pytest.mark.asyncio
async def test_fetch_search_ids_preserves_order_and_paginates(tmp_path):
    """fetch_search_ids follows cursors and preserves IMDb result order."""
    page1 = _make_graphql_response(["tt0000003", "tt0000001"], has_next_page=True, end_cursor="c1")
    page2 = _make_graphql_response(["tt0000002"], has_next_page=False)
    responses = [
        MagicMock(json=lambda d=data: d, raise_for_status=MagicMock()) for data in [page1, page2]
    ]
    mock_client = _mock_async_httpx_client(responses)

    with patch("constraints.httpx.AsyncClient", return_value=mock_client):
        ids = await constraints.fetch_search_ids(
            {"genreConstraint": {"allGenreIds": ["Drama"]}},
            sort={"sortBy": "POPULARITY", "sortOrder": "ASC"},
            page_size=2,
        )

    assert ids == ["tt0000003", "tt0000001", "tt0000002"]
    assert mock_client.post.call_count == 2


@pytest.mark.asyncio
async def test_fetch_search_ids_honours_limit(tmp_path):
    """fetch_search_ids stops collecting once limit IDs are reached."""
    page1 = _make_graphql_response(["tt0000001", "tt0000002"], has_next_page=True, end_cursor="c1")
    responses = [MagicMock(json=lambda: page1, raise_for_status=MagicMock())]
    mock_client = _mock_async_httpx_client(responses)

    with patch("constraints.httpx.AsyncClient", return_value=mock_client):
        ids = await constraints.fetch_search_ids(
            {"genreConstraint": {"allGenreIds": ["Drama"]}}, limit=1, page_size=250
        )

    assert ids == ["tt0000001"]
    assert mock_client.post.call_count == 1


@pytest.mark.asyncio
async def test_fetch_search_ids_raises_on_graphql_error(tmp_path):
    """A GraphQL error in the search response is surfaced as RuntimeError."""
    response = {"errors": [{"message": "Bad search"}]}
    mock_client = _mock_async_httpx_client(
        [MagicMock(json=lambda: response, raise_for_status=MagicMock())]
    )

    with patch("constraints.httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(RuntimeError, match="Bad search"):
            await constraints.fetch_search_ids({"bad": True})


@pytest.mark.asyncio
async def test_get_search_ids_caches_and_returns_hit(tmp_path):
    """get_search_ids caches on first call and reports a hit on the second."""
    db_path = tmp_path / "imdb.db"
    search_constraints = {"genreConstraint": {"allGenreIds": ["Drama"]}}
    response = _make_graphql_response(["tt0000001", "tt0000002"])
    mock_client = _mock_async_httpx_client(
        [MagicMock(json=lambda: response, raise_for_status=MagicMock())]
    )

    with patch("constraints.httpx.AsyncClient", return_value=mock_client):
        ids1, hit1 = await constraints.get_search_ids(db_path, search_constraints, limit=250)

    assert ids1 == ["tt0000001", "tt0000002"]
    assert hit1 is False

    # Second call must not hit GraphQL (no more mocked responses queued).
    ids2, hit2 = await constraints.get_search_ids(db_path, search_constraints, limit=250)
    assert ids2 == ["tt0000001", "tt0000002"]
    assert hit2 is True


@pytest.mark.asyncio
async def test_get_search_ids_ignore_cache_forces_refresh(tmp_path):
    """ignore_cache bypasses a fresh search cache entry."""
    db_path = tmp_path / "imdb.db"
    search_constraints = {"genreConstraint": {"allGenreIds": ["Drama"]}}
    await constraints.save_constraint_cache(
        db_path,
        "search",
        {"constraints": search_constraints, "sort": None, "limit": None},
        ["ttOLD"],
    )

    response = _make_graphql_response(["ttNEW"])
    mock_client = _mock_async_httpx_client(
        [MagicMock(json=lambda: response, raise_for_status=MagicMock())]
    )

    with patch("constraints.httpx.AsyncClient", return_value=mock_client):
        ids, hit = await constraints.get_search_ids(db_path, search_constraints, ignore_cache=True)

    assert ids == ["ttNEW"]
    assert hit is False
