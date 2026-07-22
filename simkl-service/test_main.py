"""Tests for simkl-service main.py."""

import json
import os
import shutil
from datetime import date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# Point the service at a temp directory before importing main
_TEST_DATA = Path("/tmp/test_simkl_data")
os.environ["DATA_DIR"] = str(_TEST_DATA)

import main  # noqa: E402
from main import (  # noqa: E402
    app,
    extract_items_from_list,
    fetch_and_cache_list,
    init_database,
    is_fresh,
    save_items_to_disk,
    upsert_items,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_TRENDING = {
    "movies": [{"title": "Movie A", "ids": {"simkl_id": 1001, "imdb": "tt0001", "tmdb": 501}}],
    "tv": [{"title": "Show B", "ids": {"simkl_id": 2001, "tvdb": 701}}],
    "anime": [{"title": "Anime C", "ids": {"simkl_id": 3001, "mal": 901}}],
}

SAMPLE_DVD = [
    {"title": "DVD Movie", "ids": {"simkl_id": 4001, "imdb": "tt0004"}},
]


@pytest.fixture(autouse=True)
def clean_data_dir():
    """Remove test data dir before each test."""
    if _TEST_DATA.exists():
        shutil.rmtree(_TEST_DATA)
    _TEST_DATA.mkdir(parents=True)
    # Patch module-level path constants derived from DATA_DIR
    with (
        patch.object(main, "DATA_DIR", _TEST_DATA),
        patch.object(main, "LISTS_DIR", _TEST_DATA / "lists"),
    ):
        yield
    if _TEST_DATA.exists():
        shutil.rmtree(_TEST_DATA)


@pytest.fixture
def test_client(clean_data_dir):
    """Yield a TestClient with the full app lifespan (startup/shutdown).

    Patches fetch_and_cache_list with a no-op AsyncMock so the background
    refresh worker never makes real HTTP calls to SIMKL during tests.
    Individual tests that need custom fetch behaviour re-patch the function
    inside their own ``with patch(...)`` block, which takes precedence.
    """
    with patch("main.fetch_and_cache_list", new_callable=AsyncMock):
        with TestClient(app) as client:
            yield client


@pytest.fixture
async def db(clean_data_dir):
    """Initialise the database in the temp dir."""
    await init_database()


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def make_list_file(stem: str, data: object, fresh: bool = True) -> Path:
    """Write a list JSON file, optionally backdating its mtime."""
    lists_dir = _TEST_DATA / "lists"
    lists_dir.mkdir(parents=True, exist_ok=True)
    p = lists_dir / f"{stem}.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    if not fresh:
        yesterday = datetime.now().timestamp() - 86400
        os.utime(p, (yesterday, yesterday))
    return p


def make_item_file(item_type: str, simkl_id: int, data: object) -> Path:
    """Write an individual item JSON file."""
    d = _TEST_DATA / item_type
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{simkl_id}.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# is_fresh
# ---------------------------------------------------------------------------


def test_is_fresh_missing_file():
    """Missing file is not fresh."""
    assert not is_fresh(Path("/tmp/does_not_exist_xyz.json"))


def test_is_fresh_today(clean_data_dir):
    """File modified today is fresh."""
    p = make_list_file("trending_today_100", {}, fresh=True)
    assert is_fresh(p)


def test_is_fresh_yesterday(clean_data_dir):
    """File modified yesterday is stale."""
    p = make_list_file("trending_today_100", {}, fresh=False)
    assert not is_fresh(p)


# ---------------------------------------------------------------------------
# extract_items_from_list
# ---------------------------------------------------------------------------


def test_extract_trending():
    """Trending extraction produces typed items from all three categories."""
    items = extract_items_from_list(SAMPLE_TRENDING, "trending")
    assert len(items) == 3
    types = {i["_type"] for i in items}
    assert types == {"movies", "tv", "anime"}


def test_extract_dvd():
    """DVD extraction produces movie-typed items from flat list."""
    items = extract_items_from_list(SAMPLE_DVD, "dvd")
    assert len(items) == 1
    assert items[0]["_type"] == "movies"


def test_extract_trending_empty():
    """Empty trending payload returns empty list."""
    assert extract_items_from_list({}, "trending") == []


def test_extract_dvd_non_list():
    """Non-list DVD payload returns empty list."""
    assert extract_items_from_list({}, "dvd") == []


# ---------------------------------------------------------------------------
# save_items_to_disk & upsert_items
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_items_to_disk(clean_data_dir):
    """Items are written to the correct type subdirectory."""
    items = [{"title": "Movie A", "ids": {"simkl_id": 1001}, "_type": "movies"}]
    await save_items_to_disk(items)
    dest = _TEST_DATA / "movies" / "1001.json"
    assert dest.exists()
    data = json.loads(dest.read_text())
    assert data["title"] == "Movie A"


@pytest.mark.asyncio
async def test_save_items_skips_no_simkl_id(clean_data_dir):
    """Items without a simkl_id are silently skipped."""
    items = [{"title": "No ID", "ids": {}, "_type": "movies"}]
    await save_items_to_disk(items)
    assert (
        not (_TEST_DATA / "movies").exists()
        or len(list((_TEST_DATA / "movies").glob("*.json"))) == 0
    )


@pytest.mark.asyncio
async def test_upsert_items(clean_data_dir):
    """Rows are inserted into the database."""
    await init_database()
    items = [{"title": "T", "ids": {"simkl_id": 9, "imdb": "tt9"}, "_type": "movies"}]
    await upsert_items(items)

    import aiosqlite

    async with aiosqlite.connect(_TEST_DATA / "simkl.db") as db:
        cur = await db.execute("SELECT simkl_id, imdb_id FROM items WHERE simkl_id = 9")
        row = await cur.fetchone()
    assert row is not None
    assert row[1] == "tt9"


@pytest.mark.asyncio
async def test_upsert_items_skips_no_id(clean_data_dir):
    """Items without simkl_id are not inserted."""
    await init_database()
    items = [{"title": "No", "ids": {}, "_type": "movies"}]
    await upsert_items(items)

    import aiosqlite

    async with aiosqlite.connect(_TEST_DATA / "simkl.db") as db:
        cur = await db.execute("SELECT COUNT(*) FROM items")
        row = await cur.fetchone()
    assert row[0] == 0


# ---------------------------------------------------------------------------
# fetch_and_cache_list
# ---------------------------------------------------------------------------


def _mock_httpx_response(data: object):
    """Return a context-manager mock that yields a fake httpx response."""
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = data

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)
    return mock_client


@pytest.mark.asyncio
async def test_fetch_and_cache_list_trending(clean_data_dir):
    """Fetching a trending list saves the list file and individual items."""
    await init_database()
    with patch("httpx.AsyncClient", return_value=_mock_httpx_response(SAMPLE_TRENDING)):
        await fetch_and_cache_list("trending_today_small")

    list_file = _TEST_DATA / "lists" / "trending_today_100.json"
    assert list_file.exists()
    assert (_TEST_DATA / "movies" / "1001.json").exists()
    assert (_TEST_DATA / "tv" / "2001.json").exists()
    assert (_TEST_DATA / "anime" / "3001.json").exists()


@pytest.mark.asyncio
async def test_fetch_and_cache_list_dvd(clean_data_dir):
    """Fetching a DVD list saves the list file and individual movie items."""
    await init_database()
    with patch("httpx.AsyncClient", return_value=_mock_httpx_response(SAMPLE_DVD)):
        await fetch_and_cache_list("dvd_small")

    list_file = _TEST_DATA / "lists" / "dvd_100.json"
    assert list_file.exists()
    assert (_TEST_DATA / "movies" / "4001.json").exists()


@pytest.mark.asyncio
async def test_fetch_and_cache_list_unknown_key(clean_data_dir):
    """Unknown list keys are silently ignored."""
    await fetch_and_cache_list("nonexistent_key")  # Should not raise


@pytest.mark.asyncio
async def test_fetch_and_cache_list_http_error(clean_data_dir):
    """HTTP errors are caught and logged without raising."""
    await init_database()
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=Exception("network error"))
    with patch("httpx.AsyncClient", return_value=mock_client):
        await fetch_and_cache_list("trending_today_small")  # Should not raise


# ---------------------------------------------------------------------------
# List endpoints via TestClient
# ---------------------------------------------------------------------------

LIST_ENDPOINTS = [
    ("/trending/today/small", "trending_today_100", "trending"),
    ("/trending/today/large", "trending_today_500", "trending"),
    ("/trending/week/small", "trending_week_100", "trending"),
    ("/trending/week/large", "trending_week_500", "trending"),
    ("/trending/month/small", "trending_month_100", "trending"),
    ("/trending/month/large", "trending_month_500", "trending"),
    ("/dvd/small", "dvd_100", "dvd"),
    ("/dvd/large", "dvd_500", "dvd"),
]


@pytest.mark.parametrize("endpoint,stem,kind", LIST_ENDPOINTS)
def test_list_endpoint_hit(endpoint, stem, kind, test_client):
    """Fresh cached list returns 200 with X-Cache: HIT."""
    data = SAMPLE_TRENDING if kind == "trending" else SAMPLE_DVD
    make_list_file(stem, data, fresh=True)
    resp = test_client.get(endpoint)
    assert resp.status_code == 200
    assert resp.headers.get("x-cache") == "HIT"


@pytest.mark.parametrize("endpoint,stem,kind", LIST_ENDPOINTS)
def test_list_endpoint_stale(endpoint, stem, kind, test_client):
    """Stale cached list returns 200 with X-Cache: STALE."""
    data = SAMPLE_TRENDING if kind == "trending" else SAMPLE_DVD
    make_list_file(stem, data, fresh=False)
    resp = test_client.get(endpoint)
    assert resp.status_code == 200
    assert resp.headers.get("x-cache") == "STALE"


@pytest.mark.parametrize("endpoint,stem,kind", LIST_ENDPOINTS)
def test_list_endpoint_miss(endpoint, stem, kind, test_client):
    """Missing list fetches from upstream and returns 200 with X-Cache: MISS."""
    data = SAMPLE_TRENDING if kind == "trending" else SAMPLE_DVD
    with patch("main.fetch_and_cache_list", new_callable=AsyncMock) as mock_fetch:

        async def side_effect(key):
            cfg = main.KEY_TO_CONFIG[key]
            make_list_file(cfg["stem"], data, fresh=True)

        mock_fetch.side_effect = side_effect
        resp = test_client.get(endpoint)
    assert resp.status_code == 200
    assert resp.headers.get("x-cache") == "MISS"


# ---------------------------------------------------------------------------
# Individual item endpoints
# ---------------------------------------------------------------------------


def test_get_movie_found(test_client):
    """GET /movies/{id} returns 200 when file exists."""
    make_item_file("movies", 1001, {"title": "Movie A"})
    resp = test_client.get("/movies/1001")
    assert resp.status_code == 200
    assert resp.json()["title"] == "Movie A"


def test_get_movie_not_found(test_client):
    """GET /movies/{id} returns 404 when file is missing."""
    resp = test_client.get("/movies/9999")
    assert resp.status_code == 404


def test_get_tv_found(test_client):
    """GET /tv/{id} returns 200 when file exists."""
    make_item_file("tv", 2001, {"title": "Show B"})
    resp = test_client.get("/tv/2001")
    assert resp.status_code == 200


def test_get_tv_not_found(test_client):
    """GET /tv/{id} returns 404 when file is missing."""
    assert test_client.get("/tv/9999").status_code == 404


def test_get_anime_found(test_client):
    """GET /anime/{id} returns 200 when file exists."""
    make_item_file("anime", 3001, {"title": "Anime C"})
    resp = test_client.get("/anime/3001")
    assert resp.status_code == 200


def test_get_anime_not_found(test_client):
    """GET /anime/{id} returns 404 when file is missing."""
    assert test_client.get("/anime/9999").status_code == 404


# ---------------------------------------------------------------------------
# Find endpoints
# ---------------------------------------------------------------------------


@pytest.fixture
async def populated_db(clean_data_dir):
    """DB with one row per type."""
    await init_database()
    await upsert_items(
        [
            {
                "title": "Movie A",
                "ids": {"simkl_id": 1001, "imdb": "tt0001", "tmdb": 501},
                "_type": "movies",
            },
            {"title": "Show B", "ids": {"simkl_id": 2001, "tvdb": 701}, "_type": "tv"},
            {"title": "Anime C", "ids": {"simkl_id": 3001, "mal": 901}, "_type": "anime"},
        ]
    )
    make_item_file("movies", 1001, {"title": "Movie A"})
    make_item_file("tv", 2001, {"title": "Show B"})
    make_item_file("anime", 3001, {"title": "Anime C"})


def test_movies_find_by_imdb(test_client, populated_db):
    """GET /movies/find?imdb=tt0001 returns the movie."""
    resp = test_client.get("/movies/find?imdb=tt0001")
    assert resp.status_code == 200
    assert resp.json()["title"] == "Movie A"


def test_movies_find_not_found(test_client, populated_db):
    """GET /movies/find with unknown ID returns 404."""
    assert test_client.get("/movies/find?imdb=tt9999").status_code == 404


def test_movies_find_no_params(test_client, populated_db):
    """GET /movies/find with no params returns 422."""
    assert test_client.get("/movies/find").status_code == 422


def test_tv_find_by_tvdb(test_client, populated_db):
    """GET /tv/find?tvdb=701 returns the TV show."""
    resp = test_client.get("/tv/find?tvdb=701")
    assert resp.status_code == 200
    assert resp.json()["title"] == "Show B"


def test_anime_find_by_mal(test_client, populated_db):
    """GET /anime/find?mal=901 returns the anime."""
    resp = test_client.get("/anime/find?mal=901")
    assert resp.status_code == 200
    assert resp.json()["title"] == "Anime C"


def test_anime_find_no_params(test_client, populated_db):
    """GET /anime/find with no params returns 422."""
    assert test_client.get("/anime/find").status_code == 422


# ---------------------------------------------------------------------------
# Stats & health
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/health", "/health/live"])
def test_health(test_client, path):
    """Liveness endpoints return 200."""
    resp = test_client.get(path)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_health_ready(test_client):
    resp = test_client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}


def test_stats_structure(test_client):
    """GET /stats returns the expected shape."""
    resp = test_client.get("/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "online"
    assert "lists" in body
    assert "item_counts" in body
    assert "queue_size" in body
    assert set(body["item_counts"].keys()) == {"movies", "tv", "anime"}


def test_stats_fresh_list(test_client):
    """Stats report a fresh list correctly."""
    make_list_file("trending_today_100", SAMPLE_TRENDING, fresh=True)
    body = test_client.get("/stats").json()
    assert body["lists"]["trending_today_small"]["fresh"] is True
    assert body["lists"]["trending_today_small"]["cached_date"] == date.today().isoformat()


def test_stats_item_counts(test_client):
    """Stats count individual item files correctly."""
    make_item_file("movies", 1001, {})
    make_item_file("movies", 1002, {})
    make_item_file("anime", 3001, {})
    body = test_client.get("/stats").json()
    assert body["item_counts"]["movies"] == 2
    assert body["item_counts"]["anime"] == 1
    assert body["item_counts"]["tv"] == 0


# ---------------------------------------------------------------------------
# Root page
# ---------------------------------------------------------------------------


def test_root_returns_html(test_client):
    """GET / returns an HTML page."""
    resp = test_client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "SIMKL Service" in resp.text
