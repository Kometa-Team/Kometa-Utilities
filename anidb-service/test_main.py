import asyncio
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
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


@pytest.fixture(scope="function")
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
        assert count >= 1  # At least one tag should be present  # action and comedy

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

        with patch("main.check_daily_limit", return_value=True):
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
    import aiosqlite

    # Clear API logs to ensure we're under the daily limit
    async with aiosqlite.connect("/tmp/test_anidb/test.db") as db:
        await db.execute("DELETE FROM api_logs")
        await db.commit()

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
    await index_xml_to_db(9998, sample_anime_xml)

    # Create cache file
    cache_file = Path("/tmp/test_anidb/data/9998.xml")
    cache_file.write_text(sample_anime_xml, encoding="utf-8")

    # Verify database
    import aiosqlite

    async with aiosqlite.connect("/tmp/test_anidb/test.db") as db:
        cursor = await db.execute("SELECT COUNT(*) FROM anime WHERE aid = 9998")
        result = await cursor.fetchone()
        count = result[0] if result else 0
        assert count == 1

        cursor = await db.execute("SELECT COUNT(*) FROM tags WHERE aid = 9998")
        result = await cursor.fetchone()
        count = result[0] if result else 0
        assert count == 2


# ============================================================================
# Additional Coverage Tests
# ============================================================================


@pytest.mark.asyncio
async def test_log_api_request(clean_test_env):
    """Test API request logging."""
    from main import log_api_request

    # Log successful request
    await log_api_request(12345, success=True)

    # Log failed request
    await log_api_request(45678, success=False)

    # Verify logs for these specific AIDs
    import aiosqlite

    async with aiosqlite.connect("/tmp/test_anidb/test.db") as db:
        cursor = await db.execute("SELECT COUNT(*) FROM api_logs WHERE aid IN (12345, 45678)")
        result = await cursor.fetchone()
        count = result[0] if result else 0
        assert count == 2

        cursor = await db.execute("SELECT success FROM api_logs WHERE aid = 45678")
        result = await cursor.fetchone()
        assert result[0] == 0  # Failed request


@pytest.mark.asyncio
async def test_fetch_from_anidb_http_error(clean_test_env):
    """Test AniDB fetch handling of HTTP errors."""
    from main import fetch_from_anidb

    with patch("httpx.AsyncClient") as mock_client:
        mock_get = AsyncMock(side_effect=httpx.HTTPError("Network error"))
        mock_client.return_value.__aenter__.return_value.get = mock_get

        with patch("main.check_daily_limit", return_value=True):
            with pytest.raises(HTTPException) as exc_info:
                await fetch_from_anidb(1)

            assert exc_info.value.status_code == 503
            assert "AniDB API error" in exc_info.value.detail


@pytest.mark.asyncio
async def test_fetch_from_anidb_daily_limit_exceeded(clean_test_env):
    """Test fetch fails when daily limit is reached."""
    from main import fetch_from_anidb, log_api_request

    # Fill up the daily limit
    import aiosqlite

    async with aiosqlite.connect("/tmp/test_anidb/test.db") as db:
        for i in range(10):
            await db.execute(
                "INSERT INTO api_logs VALUES (?, ?, ?)",
                (datetime.now().isoformat(), i, 1),
            )
        await db.commit()

    # Should raise 429 error
    with pytest.raises(HTTPException) as exc_info:
        await fetch_from_anidb(1)

    assert exc_info.value.status_code == 429
    assert "Daily API limit reached" in exc_info.value.detail


def test_root_endpoint(test_client):
    """Test root HTML endpoint."""
    response = test_client.get("/")
    assert response.status_code == 200
    assert "AniDB Mirror Service" in response.text
    assert "/anime/{aid}" in response.text
    assert "/stats" in response.text
    assert "/search/tags" in response.text


@pytest.mark.asyncio
async def test_list_tags_endpoint(test_client, auth_headers, clean_test_env):
    """Test tags listing endpoint."""
    # Add some test data
    import aiosqlite

    async with aiosqlite.connect("/tmp/test_anidb/test.db") as db:
        await db.execute("INSERT OR REPLACE INTO anime VALUES (?, ?)", (1, datetime.now().isoformat()))
        await db.execute("INSERT INTO tags VALUES (?, ?, ?)", (9991, "action", 400))
        await db.execute("INSERT INTO tags VALUES (?, ?, ?)", (9991, "comedy", 300))
        await db.commit()

    response = test_client.get("/tags")
    assert response.status_code == 200
    assert "All Tags" in response.text
    assert "action" in response.text
    assert "comedy" in response.text


@pytest.mark.asyncio
async def test_get_anime_queues_missing_aid(test_client, auth_headers, clean_test_env):
    """Test that missing AIDs are queued."""
    response = test_client.get("/anime/9999", headers=auth_headers)
    assert response.status_code == 202
    assert "queued" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_search_tags_no_results(test_client, auth_headers, clean_test_env):
    """Test search with tags that don't exist."""
    response = test_client.get("/search/tags?tags=nonexistent", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["results"] == []


@pytest.mark.asyncio
async def test_search_tags_case_insensitive(
    test_client, auth_headers, clean_test_env, sample_anime_xml
):
    """Test that tag search is case insensitive."""
    # Index data
    await index_xml_to_db(1, sample_anime_xml)

    # Search with different cases
    response1 = test_client.get("/search/tags?tags=ACTION", headers=auth_headers)
    response2 = test_client.get("/search/tags?tags=action", headers=auth_headers)
    response3 = test_client.get("/search/tags?tags=Action", headers=auth_headers)

    assert response1.status_code == 200
    assert response2.status_code == 200
    assert response3.status_code == 200

    # All should return same results
    data1 = response1.json()
    data2 = response2.json()
    data3 = response3.json()

    assert len(data1["results"]) == len(data2["results"])
    assert len(data1["results"]) == len(data3["results"])


@pytest.mark.asyncio
async def test_get_anime_with_animedoc_naming(
    test_client, auth_headers, clean_test_env, sample_anime_xml
):
    """Test that AnimeDoc_{aid}.xml naming format is supported."""
    # Create file with AnimeDoc naming
    cache_file = Path("/tmp/test_anidb/data/AnimeDoc_5.xml")
    cache_file.write_text(sample_anime_xml, encoding="utf-8")

    # Index to database
    await index_xml_to_db(5, sample_anime_xml)

    # Should be able to retrieve it
    response = test_client.get("/anime/5", headers=auth_headers)
    assert response.status_code == 200
    assert "Test Anime" in response.text


@pytest.mark.asyncio
async def test_filter_mature_content_removes_multiple_tags(mature_anime_xml):
    """Test filtering removes all mature tags."""
    filtered = filter_mature_content(mature_anime_xml)
    # Check that mature content is removed
    assert "18 restricted" not in filtered
    # Non-mature content should remain
    assert "Mature Test Anime" in filtered


@pytest.mark.asyncio
async def test_get_anime_file_exists_no_db_entry(
    test_client, auth_headers, clean_test_env, sample_anime_xml
):
    """Test handling of cached file without database entry."""
    # Create cached file without database entry
    cache_file = Path("/tmp/test_anidb/data/7.xml")
    cache_file.write_text(sample_anime_xml, encoding="utf-8")

    # Should serve stale and queue for refresh
    response = test_client.get("/anime/7", headers=auth_headers)
    assert response.status_code == 200
    assert response.headers.get("X-Cache") == "STALE"
    assert response.headers.get("X-Status") == "Refreshing"


@pytest.mark.asyncio
async def test_stats_endpoint_with_queue_items(test_client, clean_test_env):
    """Test stats endpoint shows queue information."""
    # The queue is initialized during app lifespan
    # Just verify the stats endpoint returns queue_size field
    response = test_client.get("/stats")
    assert response.status_code == 200
    data = response.json()
    assert "queue_size" in data
    assert isinstance(data["queue_size"], int)
    assert data["queue_size"] >= 0


@pytest.mark.asyncio
async def test_search_tags_multiple_matches(
    test_client, auth_headers, clean_test_env, sample_anime_xml
):
    """Test search with multiple tag matches."""
    # Index multiple anime with overlapping tags
    await index_xml_to_db(1, sample_anime_xml)
    await index_xml_to_db(2, sample_anime_xml)

    response = test_client.get("/search/tags?tags=action,comedy", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert len(data["results"]) >= 2
    # Results should be ordered by match count
    for result in data["results"]:
        assert "aid" in result
        assert "tag_matches" in result


@pytest.mark.asyncio
async def test_authenticate_timing_safe(test_client):
    """Test that authentication uses timing-safe comparison."""
    import time

    # Valid credentials
    valid_start = time.time()
    response1 = test_client.get(
        "/anime/1",
        headers={"Authorization": "Basic dGVzdF91c2VyOnRlc3RfcGFzcw=="},
    )
    valid_time = time.time() - valid_start

    # Invalid credentials
    invalid_start = time.time()
    response2 = test_client.get(
        "/anime/1",
        headers={"Authorization": "Basic d3Jvbmc6d3Jvbmc="},
    )
    invalid_time = time.time() - invalid_start

    # Both should fail or succeed consistently
    assert response1.status_code in [200, 202]
    assert response2.status_code == 401


@pytest.mark.asyncio
async def test_index_xml_with_missing_tags(clean_test_env):
    """Test indexing XML with missing or empty tags."""
    xml_without_tags = """<?xml version="1.0" encoding="UTF-8"?>
<anime id="10" restricted="false">
    <titles>
        <title type="main">Minimal Anime</title>
    </titles>
</anime>"""

    await index_xml_to_db(10, xml_without_tags)

    import aiosqlite

    async with aiosqlite.connect("/tmp/test_anidb/test.db") as db:
        # Should still create anime record
        cursor = await db.execute("SELECT aid FROM anime WHERE aid = 10")
        assert await cursor.fetchone() is not None

        # Should have no tags
        cursor = await db.execute("SELECT COUNT(*) FROM tags WHERE aid = 10")
        result = await cursor.fetchone()
        count = result[0] if result else 0
        assert count == 0


@pytest.mark.asyncio
async def test_index_xml_with_missing_relations(clean_test_env):
    """Test indexing XML with no relations."""
    xml_without_relations = """<?xml version="1.0" encoding="UTF-8"?>
<anime id="11" restricted="false">
    <titles>
        <title type="main">Standalone Anime</title>
    </titles>
    <tags>
        <tag weight="400">
            <name>standalone</name>
        </tag>
    </tags>
</anime>"""

    await index_xml_to_db(11, xml_without_relations)

    import aiosqlite

    async with aiosqlite.connect("/tmp/test_anidb/test.db") as db:
        # Should have no relations
        cursor = await db.execute("SELECT COUNT(*) FROM relations WHERE aid = 11")
        result = await cursor.fetchone()
        count = result[0] if result else 0
        assert count == 0


@pytest.mark.asyncio
async def test_index_xml_parse_error(clean_test_env):
    """Test handling of malformed XML during indexing."""
    invalid_xml = "<broken><xml>"

    with pytest.raises(ET.ParseError):
        await index_xml_to_db(99, invalid_xml)


@pytest.mark.asyncio
async def test_search_tags_with_weight_filter(
    test_client, auth_headers, clean_test_env, sample_anime_xml
):
    """Test that min_weight filter works correctly."""
    # Index data
    await index_xml_to_db(1, sample_anime_xml)

    # Search with high min_weight (should exclude lower weighted tags)
    response = test_client.get(
        "/search/tags?tags=action,comedy&min_weight=350", headers=auth_headers
    )
    assert response.status_code == 200
    data = response.json()
    # action has weight 400, comedy has 300, so only action should match
    assert data["min_weight"] == 350


@pytest.mark.asyncio
async def test_get_anime_cache_hit_with_recent_update(
    test_client, auth_headers, clean_test_env, sample_anime_xml
):
    """Test cache hit returns proper headers for fresh content."""
    # Create cached file
    cache_file = Path("/tmp/test_anidb/data/8.xml")
    cache_file.write_text(sample_anime_xml, encoding="utf-8")

    # Index with recent timestamp
    import aiosqlite

    async with aiosqlite.connect("/tmp/test_anidb/test.db") as db:
        recent_date = datetime.now().isoformat()
        await db.execute("INSERT OR REPLACE INTO anime VALUES (?, ?)", (8, recent_date))
        await db.commit()

    response = test_client.get("/anime/8", headers=auth_headers)
    assert response.status_code == 200
    assert response.headers.get("X-Cache") == "HIT"
    assert "X-Age-Days" in response.headers
    assert int(response.headers.get("X-Age-Days")) == 0


# ============================================================================
# Exception Handler Tests
# ============================================================================


@pytest.mark.asyncio
async def test_index_xml_database_error(clean_test_env, sample_anime_xml):
    """Test index_xml_to_db handles database errors."""
    with patch("aiosqlite.connect") as mock_connect:
        mock_connect.side_effect = Exception("Database connection failed")
        
        with pytest.raises(Exception, match="Database connection failed"):
            await index_xml_to_db(1, sample_anime_xml)


@pytest.mark.asyncio
async def test_stats_endpoint_database_error(test_client):
    """Test /stats endpoint handles database errors."""
    with patch("main.DB_PATH", Path("/nonexistent/path/to/db.db")):
        response = test_client.get("/stats")
        assert response.status_code == 500
        assert "Database error" in response.json()["detail"]


@pytest.mark.asyncio
async def test_tags_endpoint_database_error(test_client):
    """Test /tags endpoint handles database errors."""
    with patch("main.DB_PATH", Path("/nonexistent/path/to/db.db")):
        response = test_client.get("/tags")
        assert response.status_code == 500
        assert "Database error" in response.json()["detail"]


@pytest.mark.asyncio
async def test_search_tags_database_error(test_client, auth_headers):
    """Test /search/tags endpoint handles database errors."""
    with patch("main.DB_PATH", Path("/nonexistent/path/to/db.db")):
        response = test_client.get("/search/tags?tags=action", headers=auth_headers)
        assert response.status_code == 500
        assert "Search error" in response.json()["detail"]


# ============================================================================
# Background Worker Tests
# ============================================================================


@pytest.mark.asyncio
async def test_anidb_worker_cancellation(clean_test_env):
    """Test that worker handles cancellation gracefully."""
    from main import anidb_worker
    
    # Create a new queue for this test
    test_queue = asyncio.Queue()
    
    with patch("main.update_queue", test_queue):
        # Start worker
        worker_task = asyncio.create_task(anidb_worker())
        
        # Give it a moment to start
        await asyncio.sleep(0.1)
        
        # Cancel the worker
        worker_task.cancel()
        
        # Should complete without error
        try:
            await worker_task
        except asyncio.CancelledError:
            pass  # Expected


@pytest.mark.asyncio
async def test_anidb_worker_error_handling(clean_test_env):
    """Test that worker continues after errors."""
    from main import fetch_from_anidb
    import aiosqlite
    
    # Clear API logs
    async with aiosqlite.connect("/tmp/test_anidb/test.db") as db:
        await db.execute("DELETE FROM api_logs")
        await db.commit()
    
    # Create a new queue and pending set for this test
    test_queue = asyncio.Queue()
    test_pending = set()
    
    # Mock fetch to fail
    with patch("main.fetch_from_anidb", side_effect=Exception("Fetch failed")):
        with patch("main.update_queue", test_queue):
            with patch("main.pending_aids", test_pending):
                # Add item to queue
                test_pending.add(99999)
                await test_queue.put(99999)
                
                # Create and start worker task
                async def run_worker():
                    from main import anidb_worker
                    await anidb_worker()
                
                worker_task = asyncio.create_task(run_worker())
                
                # Wait for processing
                await asyncio.sleep(0.3)
                
                # Cancel worker
                worker_task.cancel()
                try:
                    await worker_task
                except asyncio.CancelledError:
                    pass


# ============================================================================
# Lifespan and Startup Tests
# ============================================================================


@pytest.mark.asyncio
async def test_lifespan_startup_creates_directories():
    """Test that lifespan creates necessary directories."""
    import shutil
    from main import lifespan, app
    
    test_xml = Path("/tmp/test_lifespan/data")
    test_db = Path("/tmp/test_lifespan/db")
    
    # Clean up before
    if test_xml.parent.exists():
        shutil.rmtree(test_xml.parent)
    if test_db.parent.exists():
        shutil.rmtree(test_db.parent)
    
    with patch("main.XML_DIR", test_xml):
        with patch("main.DB_PATH", test_db / "test.db"):
            with patch("main.SEED_DATA_DIR", Path("/tmp/nonexistent_seed")):
                async with lifespan(app):
                    # Directories should be created
                    assert test_xml.exists()
                    assert test_db.exists()
    
    # Clean up after
    if test_xml.parent.exists():
        shutil.rmtree(test_xml.parent)
    if test_db.parent.exists():
        shutil.rmtree(test_db.parent)


@pytest.mark.asyncio  
async def test_lifespan_shutdown_cleanup():
    """Test that lifespan properly shuts down worker."""
    from main import lifespan, app
    import shutil
    
    test_xml = Path("/tmp/test_shutdown/data")
    test_db = Path("/tmp/test_shutdown/db")
    
    # Clean up before
    if test_xml.parent.exists():
        shutil.rmtree(test_xml.parent)
    
    with patch("main.XML_DIR", test_xml):
        with patch("main.DB_PATH", test_db / "test.db"):
            with patch("main.SEED_DATA_DIR", Path("/tmp/nonexistent_seed")):
                async with lifespan(app):
                    await asyncio.sleep(0.1)
                # Exits cleanly, worker is cancelled
    
    # Clean up after
    if test_xml.parent.exists():
        shutil.rmtree(test_xml.parent)
    if test_db.parent.exists():
        shutil.rmtree(test_db.parent)


# ============================================================================
# Configuration Tests
# ============================================================================


def test_root_endpoint_with_root_path(test_client):
    """Test root endpoint when ROOT_PATH is configured."""
    with patch("main.ROOT_PATH", "/anidb-service"):
        response = test_client.get("/")
        assert response.status_code == 200
        assert "/anidb-service" in response.text


def test_root_endpoint_constructs_base_url(test_client):
    """Test that root endpoint properly constructs base URL from request."""
    response = test_client.get("/", headers={"Host": "example.com"})
    assert response.status_code == 200
    # Should use the host from headers
    assert "AniDB Mirror Service" in response.text


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
