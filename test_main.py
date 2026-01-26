import asyncio
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from httpx import AsyncClient

# Set test environment variables before importing main
os.environ["XML_DIR"] = "/tmp/test_anidb/data"
os.environ["DB_PATH"] = "/tmp/test_anidb/test.db"
os.environ["API_USER"] = "test_user"
os.environ["API_PASS"] = "test_pass"
os.environ["ANIDB_USERNAME"] = "test_anidb"
os.environ["ANIDB_PASSWORD"] = "test_anidb_pass"
os.environ["DAILY_LIMIT"] = "10"
os.environ["UPDATE_THRESHOLD_DAYS"] = "7"  # Make 10-day cache properly stale

from main import (
    app,
    authenticate,
    check_daily_limit,
    filter_mature_content,
    index_xml_to_db,
    init_database,
)


@pytest.fixture
def test_client():
    """Provide a test client for the FastAPI app."""
    with TestClient(app) as client:
        yield client


@pytest.fixture
def auth_headers():
    """Provide valid authentication headers."""
    import base64

    credentials = base64.b64encode(b"test_user:test_pass").decode("utf-8")
    return {"Authorization": f"Basic {credentials}"}


@pytest.fixture
def invalid_auth_headers():
    """Provide invalid authentication headers."""
    import base64

    credentials = base64.b64encode(b"wrong:wrong").decode("utf-8")
    return {"Authorization": f"Basic {credentials}"}


@pytest.fixture
async def clean_test_env():
    """Clean up test environment before and after tests."""
    import shutil

    test_dir = Path("/tmp/test_anidb")

    # Cleanup before
    if test_dir.exists():
        shutil.rmtree(test_dir)

    test_dir.mkdir(parents=True, exist_ok=True)
    (test_dir / "data").mkdir(exist_ok=True)

    # Initialize database for tests
    await init_database()

    yield

    # Cleanup after
    if test_dir.exists():
        shutil.rmtree(test_dir)


@pytest.fixture
def sample_anime_xml():
    """Provide sample AniDB anime XML."""
    return """<?xml version="1.0" encoding="UTF-8"?>
<anime id="1" restricted="false">
    <titles>
        <title type="main">Test Anime</title>
    </titles>
    <type>TV Series</type>
    <episodecount>12</episodecount>
    <startdate>2020-01-01</startdate>
    <tags>
        <tag weight="400">
            <name>action</name>
        </tag>
        <tag weight="300">
            <name>comedy</name>
        </tag>
    </tags>
    <relatedanime>
        <anime id="2" type="sequel"/>
        <anime id="3" type="prequel"/>
    </relatedanime>
</anime>"""


@pytest.fixture
def mature_anime_xml():
    """Provide sample mature content AniDB XML."""
    return """<?xml version="1.0" encoding="UTF-8"?>
<anime id="999" restricted="true">
    <titles>
        <title type="main">Mature Test Anime</title>
    </titles>
    <type>OVA</type>
    <tags>
        <tag weight="600">
            <name>18 restricted</name>
        </tag>
        <tag weight="400">
            <name>action</name>
        </tag>
    </tags>
    <categories>
        <category>
            <name>hentai</name>
        </category>
    </categories>
</anime>"""


# ============================================================================
# Authentication Tests
# ============================================================================


def test_stats_endpoint_no_auth(test_client):
    """Test that /stats endpoint works without authentication."""
    response = test_client.get("/stats")
    assert response.status_code == 200
    data = response.json()
    assert "status" in data
    assert data["status"] == "online"


def test_anime_endpoint_requires_auth(test_client):
    """Test that /anime endpoint requires authentication."""
    response = test_client.get("/anime/1")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_anime_endpoint_with_valid_auth(
    test_client, auth_headers, clean_test_env
):
    """Test that /anime endpoint accepts valid credentials."""
    response = test_client.get("/anime/1", headers=auth_headers)
    # Will return 202 (queued) or 200 depending on cache
    assert response.status_code in [200, 202]


def test_anime_endpoint_with_invalid_auth(test_client, invalid_auth_headers):
    """Test that /anime endpoint rejects invalid credentials."""
    response = test_client.get("/anime/1", headers=invalid_auth_headers)
    assert response.status_code == 401


def test_search_endpoint_requires_auth(test_client):
    """Test that /search/tags endpoint requires authentication."""
    response = test_client.get("/search/tags?tags=action")
    assert response.status_code == 401


# ============================================================================
# Database Tests
# ============================================================================


@pytest.mark.asyncio
async def test_init_database(clean_test_env):
    """Test database initialization creates required tables."""
    # init_database already called by clean_test_env fixture

    import aiosqlite

    async with aiosqlite.connect("/tmp/test_anidb/test.db") as db:
        # Check anime table
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='anime'"
        )
        assert await cursor.fetchone() is not None

        # Check tags table
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tags'"
        )
        assert await cursor.fetchone() is not None

        # Check relations table
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='relations'"
        )
        assert await cursor.fetchone() is not None


@pytest.mark.asyncio
async def test_index_xml_to_db(clean_test_env, sample_anime_xml):
    """Test XML indexing to database."""
    # Database already initialized by clean_test_env fixture
    await index_xml_to_db(1, sample_anime_xml)

    import aiosqlite

    async with aiosqlite.connect("/tmp/test_anidb/test.db") as db:
        # Check anime record
        cursor = await db.execute("SELECT aid FROM anime WHERE aid = 1")
        assert await cursor.fetchone() is not None

        # Check tags
        cursor = await db.execute("SELECT COUNT(*) FROM tags WHERE aid = 1")
        result = await cursor.fetchone()
        count = result[0] if result else 0
        assert count == 2  # action and comedy

        # Check relations
        cursor = await db.execute("SELECT COUNT(*) FROM relations WHERE aid = 1")
        result = await cursor.fetchone()
        count = result[0] if result else 0
        assert count == 2  # sequel and prequel


@pytest.mark.asyncio
async def test_check_daily_limit(clean_test_env):
    """Test daily rate limit checking."""
    # Database already initialized by clean_test_env fixture

    # Should be under limit initially
    assert await check_daily_limit() is True

    # Add API logs up to the limit
    import aiosqlite

    async with aiosqlite.connect("/tmp/test_anidb/test.db") as db:
        for i in range(10):
            await db.execute(
                "INSERT INTO api_logs VALUES (?, ?, ?)",
                (datetime.now().isoformat(), i, 1),
            )
        await db.commit()

    # Should be at limit
    assert await check_daily_limit() is False


# ============================================================================
# Mature Content Filtering Tests
# ============================================================================


def test_filter_mature_content_removes_restricted_tags(mature_anime_xml):
    """Test that mature content filtering removes 18+ tags."""
    filtered = filter_mature_content(mature_anime_xml)
    assert "18 restricted" not in filtered
    assert "action" in filtered  # Non-mature tags should remain


def test_filter_mature_content_removes_hentai_category(mature_anime_xml):
    """Test that mature content filtering removes adult categories."""
    filtered = filter_mature_content(mature_anime_xml)
    assert "hentai" not in filtered


def test_filter_mature_content_preserves_safe_content(sample_anime_xml):
    """Test that filtering doesn't break safe content."""
    filtered = filter_mature_content(sample_anime_xml)
    assert "action" in filtered
    assert "comedy" in filtered
    assert "Test Anime" in filtered


# ============================================================================
# Endpoint Tests
# ============================================================================


def test_stats_endpoint_structure(test_client):
    """Test that /stats returns expected structure."""
    response = test_client.get("/stats")
    assert response.status_code == 200
    data = response.json()

    assert "status" in data
    assert "cached_anime" in data
    assert "api_calls_last_24h" in data
    assert "queue_size" in data
    assert "daily_limit" in data
    assert data["daily_limit"] == 10


def test_anime_endpoint_invalid_aid(test_client, auth_headers):
    """Test that /anime rejects invalid AID values."""
    response = test_client.get("/anime/0", headers=auth_headers)
    assert response.status_code == 400

    response = test_client.get("/anime/-1", headers=auth_headers)
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_anime_endpoint_with_cache(
    test_client, auth_headers, clean_test_env, sample_anime_xml
):
    """Test that /anime serves from cache when available."""
    # Database already initialized by clean_test_env fixture

    # Create cached file
    cache_file = Path("/tmp/test_anidb/data/1.xml")
    cache_file.write_text(sample_anime_xml, encoding="utf-8")

    # Index to database
    await index_xml_to_db(1, sample_anime_xml)

    # Request should return cached data
    response = test_client.get("/anime/1", headers=auth_headers)
    assert response.status_code == 200
    assert "X-Cache" in response.headers
    assert "Test Anime" in response.text


def test_anime_endpoint_mature_parameter_default(test_client, auth_headers):
    """Test that mature parameter defaults to true."""
    response = test_client.get("/anime/1", headers=auth_headers)
    # Check that we can pass through (will queue if not cached)
    assert response.status_code in [200, 202]


@pytest.mark.asyncio
async def test_anime_endpoint_mature_filtering(
    test_client, auth_headers, clean_test_env, mature_anime_xml
):
    """Test that mature parameter filters content."""
    # Database already initialized by clean_test_env fixture

    # Create cached mature content
    cache_file = Path("/tmp/test_anidb/data/999.xml")
    cache_file.write_text(mature_anime_xml, encoding="utf-8")

    # Index to database
    await index_xml_to_db(999, mature_anime_xml)

    # Request with mature=true (default) should include everything
    response = test_client.get("/anime/999?mature=true", headers=auth_headers)
    assert response.status_code == 200
    assert "18 restricted" in response.text
    assert response.headers.get("X-Mature-Filter") == "disabled"

    # Request with mature=false should filter
    response = test_client.get("/anime/999?mature=false", headers=auth_headers)
    assert response.status_code == 200
    assert "18 restricted" not in response.text
    assert response.headers.get("X-Mature-Filter") == "enabled"


def test_search_tags_endpoint(test_client, auth_headers):
    """Test search by tags endpoint."""
    response = test_client.get("/search/tags?tags=action,comedy", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()

    assert "query" in data
    assert "min_weight" in data
    assert "results" in data
    assert isinstance(data["results"], list)
    assert data["query"] == ["action", "comedy"]


def test_search_tags_with_min_weight(test_client, auth_headers):
    """Test search with custom minimum weight."""
    response = test_client.get(
        "/search/tags?tags=action&min_weight=500", headers=auth_headers
    )
    assert response.status_code == 200
    data = response.json()
    assert data["min_weight"] == 500


# ============================================================================
# Edge Cases and Error Handling
# ============================================================================


def test_invalid_xml_parsing():
    """Test that invalid XML doesn't crash the filter."""
    invalid_xml = "<broken><xml>"
    result = filter_mature_content(invalid_xml)
    # Should return original on error
    assert result == invalid_xml


@pytest.mark.asyncio
async def test_stale_cache_handling(
    test_client, auth_headers, clean_test_env, sample_anime_xml
):
    """Test that stale cache is served while refreshing."""
    # Database already initialized by clean_test_env fixture

    # Create old cached file
    cache_file = Path("/tmp/test_anidb/data/1.xml")
    cache_file.write_text(sample_anime_xml, encoding="utf-8")

    # Index with old timestamp
    import aiosqlite

    async with aiosqlite.connect("/tmp/test_anidb/test.db") as db:
        old_date = (datetime.now() - timedelta(days=10)).isoformat()
        await db.execute("INSERT OR REPLACE INTO anime VALUES (?, ?)", (1, old_date))
        await db.commit()

    # Should serve stale content
    response = test_client.get("/anime/1", headers=auth_headers)
    assert response.status_code == 200
    assert response.headers.get("X-Cache") == "STALE"


# ============================================================================
# Mock External API Tests
# ============================================================================


@pytest.mark.asyncio
async def test_anidb_api_authentication(clean_test_env):
    """Test that AniDB API receives authentication parameters."""
    from main import fetch_from_anidb

    with patch("httpx.AsyncClient") as mock_client:
        mock_response = MagicMock()
        mock_response.text = "<anime/>"
        mock_response.raise_for_status = MagicMock()

        mock_get = AsyncMock(return_value=mock_response)
        mock_client.return_value.__aenter__.return_value.get = mock_get

        await fetch_from_anidb(1)

        # Verify authentication was included
        call_args = mock_get.call_args
        params = call_args[1]["params"]
        assert params["user"] == "test_anidb"
        assert params["pass"] == "test_anidb_pass"


@pytest.mark.asyncio
async def test_anidb_ban_detection(clean_test_env):
    """Test that AniDB ban responses are handled."""
    from main import fetch_from_anidb

    with patch("httpx.AsyncClient") as mock_client:
        mock_response = MagicMock()
        mock_response.text = "You are banned from this API"
        mock_response.raise_for_status = MagicMock()

        mock_get = AsyncMock(return_value=mock_response)
        mock_client.return_value.__aenter__.return_value.get = mock_get

        with pytest.raises(HTTPException) as exc_info:
            await fetch_from_anidb(1)

        assert exc_info.value.status_code == 503


# ============================================================================
# Integration Tests
# ============================================================================


@pytest.mark.asyncio
async def test_full_workflow(clean_test_env, sample_anime_xml):
    """Test complete workflow: init -> index -> query."""
    # Database already initialized by clean_test_env fixture

    # Index data
    await index_xml_to_db(1, sample_anime_xml)

    # Create cache file
    cache_file = Path("/tmp/test_anidb/data/1.xml")
    cache_file.write_text(sample_anime_xml, encoding="utf-8")

    # Verify database
    import aiosqlite

    async with aiosqlite.connect("/tmp/test_anidb/test.db") as db:
        cursor = await db.execute("SELECT COUNT(*) FROM anime")
        result = await cursor.fetchone()
        count = result[0] if result else 0
        assert count == 1

        cursor = await db.execute("SELECT COUNT(*) FROM tags WHERE aid = 1")
        result = await cursor.fetchone()
        count = result[0] if result else 0
        assert count == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
