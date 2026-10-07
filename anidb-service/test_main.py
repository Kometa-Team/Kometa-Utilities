import asyncio
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from fastapi.testclient import TestClient

# Set test environment variables before importing main
os.environ["XML_DIR"] = "/tmp/test_anidb/data"
os.environ["DB_PATH"] = "/tmp/test_anidb/test.db"
os.environ["ANIDB_USERNAME"] = "test_anidb"
os.environ["ANIDB_PASSWORD"] = "test_anidb_pass"
os.environ["DAILY_LIMIT"] = "10"
os.environ["UPDATE_THRESHOLD_DAYS"] = "7"  # Fallback threshold for unparseable XML

from main import (  # noqa: E402
    app,
    check_daily_limit,
    filter_mature_content,
    index_xml_to_db,
    init_database,
)


@pytest.fixture
def test_client(clean_test_env):
    """Provide a test client for the FastAPI app."""
    with TestClient(app) as client:
        # Let startup's background seed indexing run against the empty data dir, so it
        # can't index (and freshen) files that tests write afterwards
        time.sleep(0.3)
        yield client


def test_health_endpoints(test_client):
    live = test_client.get("/health/live")
    ready = test_client.get("/health/ready")
    assert live.status_code == 200
    assert live.json() == {"status": "ok"}
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}


@pytest_asyncio.fixture(scope="function")
async def clean_test_env():
    """Clean up test environment before and after tests."""
    import shutil

    test_dir = Path("/tmp/test_anidb")

    # Cleanup before
    if test_dir.exists():
        shutil.rmtree(test_dir)

    test_dir.mkdir(parents=True, exist_ok=True)
    (test_dir / "data").mkdir(exist_ok=True)

    # Ensure database file can be created (create it directly)
    # This is needed because aiosqlite sometimes has issues with the parent directory
    db_path = Path("/tmp/test_anidb/test.db")
    db_path.touch()

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


# Authentication tests removed - API no longer requires authentication
# def test_anime_endpoint_requires_auth(test_client):
#     """Test that /anime endpoint requires authentication."""
#     response = test_client.get("/anime/1")
#     assert response.status_code == 401


# @pytest.mark.asyncio
# async def test_anime_endpoint_with_valid_auth(
#     test_client, clean_test_env
# ):
#     """Test that /anime endpoint accepts valid credentials."""
#     response = test_client.get("/anime/1")
#     # Will return 202 (queued) or 200 depending on cache
#     assert response.status_code in [200, 202]


# def test_anime_endpoint_with_invalid_auth(test_client):
#     """Test that /anime endpoint rejects invalid credentials."""
#     response = test_client.get("/anime/1")
#     assert response.status_code == 401


# def test_search_endpoint_requires_auth(test_client):
#     """Test that /search/tags endpoint requires authentication."""
#     response = test_client.get("/search/tags?tags=action")
#     assert response.status_code == 401


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
    assert "rate_limit_until" in data
    assert data["daily_limit"] == 10
    assert data["rate_limit_until"] is None  # not rate-limited by default


def test_anime_endpoint_invalid_aid(test_client):
    """Test that /anime rejects invalid AID values."""
    response = test_client.get("/anime/0")
    assert response.status_code == 400

    response = test_client.get("/anime/-1")
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_anime_endpoint_with_cache(test_client, clean_test_env, sample_anime_xml):
    """Test that /anime serves from cache when available."""
    # Database already initialized by clean_test_env fixture

    # Create cached file
    cache_file = Path("/tmp/test_anidb/data/1.xml")
    cache_file.write_text(sample_anime_xml, encoding="utf-8")

    # Index to database
    await index_xml_to_db(1, sample_anime_xml)

    # Request should return cached data
    response = test_client.get("/anime/1")
    assert response.status_code == 200
    assert "X-Cache" in response.headers
    assert "Test Anime" in response.text


def test_anime_endpoint_mature_parameter_default(test_client):
    """Test that mature parameter defaults to false."""
    response = test_client.get("/anime/1")
    # Check that we can pass through (will queue if not cached)
    assert response.status_code in [200, 202]


@pytest.mark.asyncio
async def test_anime_endpoint_mature_filtering(test_client, clean_test_env, mature_anime_xml):
    """Test that mature parameter filters content."""
    # Database already initialized by clean_test_env fixture

    # Create cached mature content
    cache_file = Path("/tmp/test_anidb/data/999.xml")
    cache_file.write_text(mature_anime_xml, encoding="utf-8")

    # Index to database
    await index_xml_to_db(999, mature_anime_xml)

    # Request with mature=true (default) should include everything
    response = test_client.get("/anime/999?mature=true")
    assert response.status_code == 200
    assert "18 restricted" in response.text
    assert response.headers.get("X-Mature-Filter") == "disabled"

    # Request with mature=false should filter
    response = test_client.get("/anime/999?mature=false")
    assert response.status_code == 200
    assert "18 restricted" not in response.text
    assert response.headers.get("X-Mature-Filter") == "enabled"


def test_search_tags_endpoint(test_client):
    """Test search by tags endpoint."""
    response = test_client.get("/search/tags?tags=action,comedy")
    assert response.status_code == 200
    data = response.json()

    assert "query" in data
    assert "min_weight" in data
    assert "results" in data
    assert isinstance(data["results"], list)
    assert data["query"] == ["action", "comedy"]


def test_search_tags_with_min_weight(test_client):
    """Test search with custom minimum weight."""
    response = test_client.get("/search/tags?tags=action&min_weight=500")
    assert response.status_code == 200
    data = response.json()
    assert data["min_weight"] == 500


@pytest.mark.asyncio
async def test_search_tags_excludes_mature_content(test_client, clean_test_env):
    """Test that mature=false excludes anime with adult tags."""
    import aiosqlite

    # Create test anime - one normal, one mature
    async with aiosqlite.connect("/tmp/test_anidb/test.db") as db:
        await db.execute(
            "INSERT OR REPLACE INTO anime VALUES (?, ?)", (100, datetime.now().isoformat())
        )
        await db.execute(
            "INSERT OR REPLACE INTO anime VALUES (?, ?)", (200, datetime.now().isoformat())
        )

        # Normal anime with action tag
        await db.execute("INSERT INTO tags VALUES (?, ?, ?, ?)", (100, None, "action", 400))

        # Mature anime with action tag + 18 restricted tag
        await db.execute("INSERT INTO tags VALUES (?, ?, ?, ?)", (200, None, "action", 400))
        await db.execute("INSERT INTO tags VALUES (?, ?, ?, ?)", (200, None, "18 restricted", 600))

        await db.commit()

    # Search with mature=false (default) - should exclude mature anime
    response = test_client.get("/search/tags?tags=action")
    assert response.status_code == 200
    data = response.json()
    assert data["mature"] is False
    aids = [r["aid"] for r in data["results"]]
    assert 100 in aids
    assert 200 not in aids  # Mature anime excluded by default

    # Search with mature=true - should include all anime
    response = test_client.get("/search/tags?tags=action&mature=true")
    assert response.status_code == 200
    data = response.json()
    assert data["mature"] is True
    aids = [r["aid"] for r in data["results"]]
    assert 100 in aids
    assert 200 in aids  # Mature anime included when explicitly requested


@pytest.mark.asyncio
async def test_search_tags_mature_keywords(test_client, clean_test_env):
    """Test that all mature keywords are filtered."""
    import aiosqlite

    async with aiosqlite.connect("/tmp/test_anidb/test.db") as db:
        # Create anime with different mature tags
        for aid, mature_tag in [(301, "hentai"), (302, "pornography"), (303, "adult")]:
            await db.execute(
                "INSERT OR REPLACE INTO anime VALUES (?, ?)", (aid, datetime.now().isoformat())
            )
            await db.execute("INSERT INTO tags VALUES (?, ?, ?, ?)", (aid, None, "action", 400))
            await db.execute("INSERT INTO tags VALUES (?, ?, ?, ?)", (aid, None, mature_tag, 500))

        # Normal anime
        await db.execute(
            "INSERT OR REPLACE INTO anime VALUES (?, ?)", (400, datetime.now().isoformat())
        )
        await db.execute("INSERT INTO tags VALUES (?, ?, ?, ?)", (400, None, "action", 400))

        await db.commit()

    # With mature=false (default), all mature anime should be excluded
    response = test_client.get("/search/tags?tags=action")
    assert response.status_code == 200
    data = response.json()
    aids = [r["aid"] for r in data["results"]]

    # Only normal anime should be returned
    assert 400 in aids
    assert 301 not in aids  # hentai
    assert 302 not in aids  # pornography
    assert 303 not in aids  # adult


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
async def test_stale_cache_handling(test_client, clean_test_env, sample_anime_xml):
    """Test that stale cache is served while refreshing."""
    # Database already initialized by clean_test_env fixture

    # Create old cached file
    cache_file = Path("/tmp/test_anidb/data/1.xml")
    cache_file.write_text(sample_anime_xml, encoding="utf-8")

    # Index with old timestamp
    import aiosqlite

    async with aiosqlite.connect("/tmp/test_anidb/test.db") as db:
        old_date = (datetime.now() - timedelta(days=30)).isoformat()
        await db.execute("INSERT OR REPLACE INTO anime VALUES (?, ?)", (1, old_date))
        await db.commit()

    # Should serve stale content
    response = test_client.get("/anime/1")
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
    import aiosqlite

    from main import fetch_from_anidb

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
    # Fill up the daily limit
    import aiosqlite

    from main import fetch_from_anidb

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
    assert "/logo.png" in response.text


def test_logo_endpoint(test_client):
    response = test_client.get("/logo.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "public, max-age=86400"
    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")


@pytest.mark.asyncio
async def test_list_tags_endpoint(test_client, clean_test_env):
    """Test tags listing endpoint."""
    # Add some test data
    import aiosqlite

    async with aiosqlite.connect("/tmp/test_anidb/test.db") as db:
        await db.execute(
            "INSERT OR REPLACE INTO anime VALUES (?, ?)", (1, datetime.now().isoformat())
        )
        await db.execute("INSERT INTO tags VALUES (?, ?, ?, ?)", (9991, None, "action", 400))
        await db.execute("INSERT INTO tags VALUES (?, ?, ?, ?)", (9991, None, "comedy", 300))
        await db.commit()

    response = test_client.get("/tags")
    assert response.status_code == 200
    assert "All Tags" in response.text
    assert "action" in response.text
    assert "comedy" in response.text


@pytest.mark.asyncio
async def test_get_anime_queues_missing_aid(test_client, clean_test_env):
    """Test that missing AIDs are queued."""
    response = test_client.get("/anime/9999")
    assert response.status_code == 202
    assert "queued" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_search_tags_no_results(test_client, clean_test_env):
    """Test search with tags that don't exist."""
    response = test_client.get("/search/tags?tags=nonexistent")
    assert response.status_code == 200
    data = response.json()
    assert data["results"] == []


@pytest.mark.asyncio
async def test_search_tags_case_insensitive(test_client, clean_test_env, sample_anime_xml):
    """Test that tag search is case insensitive."""
    # Index data
    await index_xml_to_db(1, sample_anime_xml)

    # Search with different cases
    response1 = test_client.get("/search/tags?tags=ACTION")
    response2 = test_client.get("/search/tags?tags=action")
    response3 = test_client.get("/search/tags?tags=Action")

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
async def test_get_anime_with_animedoc_naming(test_client, clean_test_env, sample_anime_xml):
    """Test that AnimeDoc_{aid}.xml naming format is supported."""
    # Create file with AnimeDoc naming
    cache_file = Path("/tmp/test_anidb/data/AnimeDoc_5.xml")
    cache_file.write_text(sample_anime_xml, encoding="utf-8")

    # Index to database
    await index_xml_to_db(5, sample_anime_xml)

    # Should be able to retrieve it
    response = test_client.get("/anime/5")
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
async def test_get_anime_file_exists_no_db_entry(test_client, clean_test_env, sample_anime_xml):
    """Test handling of cached file without database entry."""
    # Create cached file without database entry
    cache_file = Path("/tmp/test_anidb/data/7.xml")
    cache_file.write_text(sample_anime_xml, encoding="utf-8")

    # Should serve stale and queue for refresh
    response = test_client.get("/anime/7")
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
async def test_search_tags_multiple_matches(test_client, clean_test_env, sample_anime_xml):
    """Test search with multiple tag matches."""
    # Index multiple anime with overlapping tags
    await index_xml_to_db(1, sample_anime_xml)
    await index_xml_to_db(2, sample_anime_xml)

    response = test_client.get("/search/tags?tags=action,comedy")
    assert response.status_code == 200
    data = response.json()
    assert len(data["results"]) >= 2
    # Results should be ordered by match count
    for result in data["results"]:
        assert "aid" in result
        assert "tag_matches" in result


# @pytest.mark.asyncio
# async def test_authenticate_timing_safe(test_client):
#     """Test that authentication uses timing-safe comparison."""
#     import time
#
#     # Valid credentials
#     valid_start = time.time()
#     response1 = test_client.get(
#         "/anime/1",
#         headers={"Authorization": "Basic dGVzdF91c2VyOnRlc3RfcGFzcw=="},
#     )
#     valid_time = time.time() - valid_start
#
#     # Invalid credentials
#     invalid_start = time.time()
#     response2 = test_client.get(
#         "/anime/1",
#         headers={"Authorization": "Basic d3Jvbmc6d3Jvbmc="},
#     )
#     invalid_time = time.time() - invalid_start
#
#     # Both should fail or succeed consistently
#     assert response1.status_code in [200, 202]
#     assert response2.status_code == 401


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
async def test_search_tags_with_weight_filter(test_client, clean_test_env, sample_anime_xml):
    """Test that min_weight filter works correctly."""
    # Index data
    await index_xml_to_db(1, sample_anime_xml)

    # Search with high min_weight (should exclude lower weighted tags)
    response = test_client.get("/search/tags?tags=action,comedy&min_weight=350")
    assert response.status_code == 200
    data = response.json()
    # action has weight 400, comedy has 300, so only action should match
    assert data["min_weight"] == 350


@pytest.mark.asyncio
async def test_get_anime_cache_hit_with_recent_update(
    test_client, clean_test_env, sample_anime_xml
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

    response = test_client.get("/anime/8")
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
async def test_search_tags_database_error(test_client):
    """Test /search/tags endpoint handles database errors."""
    with patch("main.DB_PATH", Path("/nonexistent/path/to/db.db")):
        response = test_client.get("/search/tags?tags=action")
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
async def test_anidb_worker_429_sets_rate_limit(clean_test_env):
    """Test that a 429 from AniDB sets rate_limit_until and re-queues the aid."""
    import main

    test_queue = asyncio.Queue()
    test_pending = set()

    rate_limit_exc = HTTPException(status_code=429, detail="AniDB rate limit")

    with patch("main.fetch_from_anidb", side_effect=rate_limit_exc):
        with patch("main.update_queue", test_queue):
            with patch("main.pending_aids", test_pending):
                with patch.object(main, "rate_limit_until", None):
                    test_pending.add(1)
                    await test_queue.put(1)

                    worker_task = asyncio.create_task(main.anidb_worker())

                    # Let the worker process the item and hit the 429
                    await asyncio.sleep(0.2)

                    worker_task.cancel()
                    try:
                        await worker_task
                    except asyncio.CancelledError:
                        pass

                    # The aid must have been re-queued
                    assert not test_queue.empty()
                    requeued = test_queue.get_nowait()
                    assert requeued == 1

                    # rate_limit_until must be set on the module
                    assert main.rate_limit_until is not None


@pytest.mark.asyncio
async def test_anidb_worker_error_handling(clean_test_env):
    """Test that worker continues after errors."""
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

    from main import app, lifespan

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
    import shutil

    from main import app, lifespan

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


# ============================================================================
# Queue Lane Tests
# ============================================================================


@pytest.fixture
def clean_priority_state():
    """Give a test fresh new/refresh queues and bookkeeping; yields (new_queue, refresh_queue)."""
    import main

    main.pending_aids.clear()
    main.new_aids.clear()
    main.new_lookups.clear()
    main.last_limit_warning.clear()
    for reason in main.rejected_lookups:
        main.rejected_lookups[reason] = 0
    # A fresh event keeps the app's own startup worker (blocked on the old one) asleep
    with patch("main.update_queue", asyncio.Queue()) as new_q:
        with patch("main.refresh_queue", asyncio.Queue()) as refresh_q:
            with patch("main.work_event", asyncio.Event()):
                yield new_q, refresh_q
    main.pending_aids.clear()
    main.new_aids.clear()


async def _run_worker(seconds=0.3):
    import main

    task = asyncio.create_task(main.anidb_worker())
    await asyncio.sleep(seconds)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_uncached_aid_goes_to_new_lane(test_client, clean_test_env, clean_priority_state):
    """A request for an uncached AID is queued in the new lane."""
    import main

    new_q, refresh_q = clean_priority_state
    assert test_client.get("/anime/777").status_code == 202
    assert new_q.get_nowait() == 777
    assert refresh_q.empty()
    assert 777 in main.new_aids


@pytest.mark.asyncio
async def test_stale_request_queues_refresh_even_while_new_waiting(
    test_client, clean_test_env, clean_priority_state, sample_anime_xml
):
    """A stale entry is always added to the refresh lane, behind any new AIDs."""
    import aiosqlite

    import main

    new_q, refresh_q = clean_priority_state
    Path("/tmp/test_anidb/data/1.xml").write_text(sample_anime_xml, encoding="utf-8")
    async with aiosqlite.connect("/tmp/test_anidb/test.db") as db:
        old = (datetime.now() - timedelta(days=30)).isoformat()
        await db.execute("INSERT OR REPLACE INTO anime VALUES (?, ?)", (1, old))
        await db.commit()

    await main.enqueue_aid(999, is_new=True)  # uncached AID already waiting
    response = test_client.get("/anime/1")
    assert response.status_code == 200
    assert response.headers["X-Cache"] == "STALE"
    assert refresh_q.get_nowait() == 1
    assert new_q.get_nowait() == 999


@pytest.mark.asyncio
async def test_worker_drains_new_lane_before_refresh_lane(clean_test_env, clean_priority_state):
    """Refreshes wait until the new lane is empty, even if they were queued first."""
    import main

    await main.enqueue_aid(1, is_new=False)
    await main.enqueue_aid(2, is_new=True)
    await main.enqueue_aid(3, is_new=False)
    await main.enqueue_aid(4, is_new=True)

    fetch = AsyncMock(return_value="<anime/>")
    with patch("main.fetch_from_anidb", fetch), patch("main.THROTTLE_SECONDS", 0):
        await _run_worker()

    assert [c.args[0] for c in fetch.call_args_list] == [2, 4, 1, 3]


@pytest.mark.asyncio
async def test_worker_wakes_for_work_queued_while_idle(clean_test_env, clean_priority_state):
    """An idle worker picks up an AID queued later, from either lane."""
    import main

    fetch = AsyncMock(return_value="<anime/>")
    with patch("main.fetch_from_anidb", fetch), patch("main.THROTTLE_SECONDS", 0):
        task = asyncio.create_task(main.anidb_worker())
        await asyncio.sleep(0.1)  # worker is now waiting on empty queues
        await main.enqueue_aid(8, is_new=False)
        await asyncio.sleep(0.2)
        await main.enqueue_aid(9, is_new=True)
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert [c.args[0] for c in fetch.call_args_list] == [8, 9]


@pytest.mark.asyncio
async def test_promoted_refresh_is_fetched_once(
    test_client, clean_test_env, clean_priority_state
):
    """A queued refresh whose cache file vanished is fetched once, as a new AID."""
    import main

    new_q, refresh_q = clean_priority_state
    await main.enqueue_aid(31, is_new=False)
    assert test_client.get("/anime/31").status_code == 202  # no cache file: promoted
    assert 31 in main.new_aids
    assert await _pending_rows() == [(31, "new")]

    fetch = AsyncMock(return_value="<anime/>")
    with patch("main.fetch_from_anidb", fetch), patch("main.THROTTLE_SECONDS", 0):
        await _run_worker()

    assert fetch.await_count == 1  # the leftover refresh-lane entry is skipped


def test_cached_xml_path_prefers_existing_file(clean_test_env):
    """Refreshes target the existing cache file (seed or live) instead of adding a copy."""
    import main

    data = Path("/tmp/test_anidb/data")
    assert main.cached_xml_path(5) == data / "5.xml"  # nothing cached: new live-fetch name

    (data / "AnimeDoc_5.xml").write_text("<anime/>")
    assert main.cached_xml_path(5) == data / "AnimeDoc_5.xml"  # seed file is reused

    (data / "5.xml").write_text("<anime/>")
    assert main.cached_xml_path(5) == data / "5.xml"  # existing duplicates: newer name wins


@pytest.mark.asyncio
async def test_worker_overwrites_seed_file(clean_test_env, clean_priority_state):
    """A refresh of a seed-only AID rewrites AnimeDoc_{aid}.xml and creates no new file."""
    import main

    seed = Path("/tmp/test_anidb/data/AnimeDoc_9.xml")
    seed.write_text("<anime id='9'>old</anime>")
    await main.enqueue_aid(9, is_new=False)

    with patch("main.fetch_from_anidb", AsyncMock(return_value="<anime id='9'>new</anime>")):
        with patch("main.THROTTLE_SECONDS", 0):
            await _run_worker()

    assert "new" in seed.read_text()
    assert not Path("/tmp/test_anidb/data/9.xml").exists()


# ============================================================================
# Durable Queue and Queue Stats Tests
# ============================================================================


async def _pending_rows():
    import aiosqlite

    async with aiosqlite.connect("/tmp/test_anidb/test.db") as db:
        cursor = await db.execute("SELECT aid, kind FROM pending_queue ORDER BY aid")
        return await cursor.fetchall()


@pytest.mark.asyncio
async def test_enqueue_persists_and_forget_removes(clean_test_env, clean_priority_state):
    """Queued AIDs are mirrored to the DB with their kind and removed when handled."""
    import main

    await main.enqueue_aid(1, is_new=True)
    await main.enqueue_aid(2, is_new=False)
    assert await _pending_rows() == [(1, "new"), (2, "refresh")]

    await main.forget_pending(1)
    assert await _pending_rows() == [(2, "refresh")]


@pytest.mark.asyncio
async def test_restore_queue_rebuilds_both_lanes(clean_test_env, clean_priority_state):
    """After a restart each persisted AID returns to its own lane."""
    import main

    await main.enqueue_aid(10, is_new=False)
    await main.enqueue_aid(20, is_new=True)
    await main.enqueue_aid(30, is_new=False)

    new_q, refresh_q = asyncio.Queue(), asyncio.Queue()
    main.pending_aids.clear()
    main.new_aids.clear()
    with patch("main.update_queue", new_q), patch("main.refresh_queue", refresh_q):
        await main.restore_queue()

    assert [new_q.get_nowait() for _ in range(new_q.qsize())] == [20]
    assert [refresh_q.get_nowait() for _ in range(refresh_q.qsize())] == [10, 30]
    assert main.new_aids == {20}
    assert main.pending_aids == {10, 20, 30}


@pytest.mark.asyncio
async def test_worker_clears_persisted_row_after_caching(clean_test_env, clean_priority_state):
    """A processed AID leaves the persisted queue."""
    import main

    await main.enqueue_aid(4, is_new=True)
    with patch("main.fetch_from_anidb", AsyncMock(return_value="<anime id='4'/>")):
        with patch("main.THROTTLE_SECONDS", 0):
            await _run_worker()
    assert await _pending_rows() == []


@pytest.mark.asyncio
async def test_429_keeps_row_and_persists_backoff(clean_test_env, clean_priority_state):
    """A 429 leaves the AID persisted in its lane and the back-off survives a restart."""
    import main

    new_q, refresh_q = clean_priority_state
    with patch.object(main, "rate_limit_until", None):
        await main.enqueue_aid(5, is_new=False)
        limited = HTTPException(status_code=429, detail="limit")
        with patch("main.fetch_from_anidb", side_effect=limited):
            await _run_worker()
        assert await _pending_rows() == [(5, "refresh")]
        assert refresh_q.qsize() == 1 and new_q.empty()
        paused_until = main.rate_limit_until
        assert paused_until is not None

    # Simulate a restart: module state is gone, the DB still has it
    with patch("main.update_queue", asyncio.Queue()), patch("main.refresh_queue", asyncio.Queue()):
        with patch.object(main, "rate_limit_until", None):
            await main.restore_queue()
            assert main.rate_limit_until == paused_until


@pytest.mark.asyncio
async def test_stats_reports_new_vs_refresh(test_client, clean_test_env, clean_priority_state):
    """/stats breaks the backlog down into uncached and refresh entries."""
    import main

    await main.enqueue_aid(1, is_new=True)
    await main.enqueue_aid(2, is_new=True)
    await main.enqueue_aid(3, is_new=False)
    data = test_client.get("/stats").json()
    assert data["queued_new"] == 2
    assert data["queued_refresh"] == 1
    assert data["queue_size"] == 3


# ============================================================================
# Tiered Refresh Threshold Tests
# ============================================================================


def _dated_xml(start=None, end=None):
    parts = []
    if start:
        parts.append(f"<startdate>{start}</startdate>")
    if end:
        parts.append(f"<enddate>{end}</enddate>")
    return f"<anime id='1'>{''.join(parts)}</anime>"


@pytest.mark.parametrize(
    "start,end,days",
    [
        (None, None, 30),  # no start date
        ("2099-01-01", None, 14),  # not yet started
        ("2024-01-01", None, 14),  # airing / open-ended
        ("2024-01-01", "2099-01-01", 14),  # end date in the future
        ("2024-01-01", "2026-06-01", 30),  # ended < 1 year ago
        ("2020-01-01", "2024-10-01", 90),  # ended 1-3 years ago
        ("2010-01-01", "2020-10-01", 180),  # ended 3-10 years ago
        ("1990-01-01", "2000-10-01", 365),  # ended 10+ years ago
        ("1990", "2000-10", 365),  # partial dates
    ],
)
def test_refresh_threshold_tiers(start, end, days):
    """Entries are refreshed less often the longer ago they finished airing."""
    import main

    now = datetime(2026, 10, 7)
    assert main.refresh_threshold(_dated_xml(start, end), now) == timedelta(days=days)


def test_refresh_threshold_unparseable_uses_fallback():
    """Broken XML falls back to UPDATE_THRESHOLD_DAYS."""
    import main

    assert main.refresh_threshold("<anime>", datetime(2026, 10, 7)) == main.UPDATE_THRESHOLD


@pytest.mark.asyncio
async def test_old_finished_anime_stays_fresh(test_client, clean_test_env, clean_priority_state):
    """A long-finished entry 30 days old is a cache hit and is not queued for refresh."""
    import aiosqlite

    import main

    Path("/tmp/test_anidb/data/1.xml").write_text(
        _dated_xml("1990-01-01", "1991-01-01"), encoding="utf-8"
    )
    async with aiosqlite.connect("/tmp/test_anidb/test.db") as db:
        old = (datetime.now() - timedelta(days=30)).isoformat()
        await db.execute("INSERT OR REPLACE INTO anime VALUES (?, ?)", (1, old))
        await db.commit()

    new_q, refresh_q = clean_priority_state
    response = test_client.get("/anime/1")
    assert response.headers["X-Cache"] == "HIT"
    assert response.headers["X-Refresh-After-Days"] == "365"
    assert new_q.empty() and refresh_q.empty()


# ============================================================================
# Queue De-duplication Tests
# ============================================================================


@pytest.mark.asyncio
async def test_repeat_requests_queue_a_stale_aid_once(
    test_client, clean_test_env, clean_priority_state, sample_anime_xml
):
    """Many requests for the same stale AID produce a single queue entry."""
    import aiosqlite

    new_q, refresh_q = clean_priority_state
    Path("/tmp/test_anidb/data/1234.xml").write_text(sample_anime_xml, encoding="utf-8")
    async with aiosqlite.connect("/tmp/test_anidb/test.db") as db:
        old = (datetime.now() - timedelta(days=30)).isoformat()
        await db.execute("INSERT OR REPLACE INTO anime VALUES (?, ?)", (1234, old))
        await db.commit()

    for _ in range(5):
        assert test_client.get("/anime/1234").headers["X-Cache"] == "STALE"
    assert refresh_q.qsize() == 1
    assert await _pending_rows() == [(1234, "refresh")]


@pytest.mark.asyncio
async def test_requests_during_fetch_do_not_requeue(
    test_client, clean_test_env, clean_priority_state, sample_anime_xml
):
    """While the worker is fetching an AID, further requests for it are not re-queued."""
    import aiosqlite

    import main

    new_q, refresh_q = clean_priority_state
    Path("/tmp/test_anidb/data/1234.xml").write_text(sample_anime_xml, encoding="utf-8")
    async with aiosqlite.connect("/tmp/test_anidb/test.db") as db:
        old = (datetime.now() - timedelta(days=30)).isoformat()
        await db.execute("INSERT OR REPLACE INTO anime VALUES (?, ?)", (1234, old))
        await db.commit()

    fetching = asyncio.Event()
    release = asyncio.Event()

    async def slow_fetch(aid):
        fetching.set()
        await release.wait()
        return "<anime id='1234'/>"

    await main.enqueue_aid(1234, is_new=False)
    with patch("main.fetch_from_anidb", slow_fetch), patch("main.THROTTLE_SECONDS", 0):
        task = asyncio.create_task(main.anidb_worker())
        await asyncio.wait_for(fetching.wait(), timeout=2)  # worker is mid-fetch

        assert test_client.get("/anime/1234").headers["X-Cache"] == "STALE"
        assert refresh_q.empty() and new_q.empty()

        release.set()
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ============================================================================
# Abuse Protection Tests
# ============================================================================


def _req(headers=None, host="10.0.0.9"):
    from starlette.requests import Request

    scope = {
        "type": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": (host, 1234),
    }
    return Request(scope)


def test_client_key_uses_last_forwarded_value():
    """The entry added by the nearest proxy wins, so client-supplied values can't spoof it."""
    import main

    assert main.client_key(_req({"X-Forwarded-For": "6.6.6.6, 203.0.113.5"})) == "203.0.113.5"
    assert main.client_key(_req({"X-Forwarded-For": "203.0.113.5"})) == "203.0.113.5"


def test_client_key_fallbacks_and_ipv6():
    """Falls back to the socket peer; IPv6 callers share a /64 bucket."""
    import main

    assert main.client_key(_req()) == "10.0.0.9"
    assert main.client_key(_req({"X-Forwarded-For": "not-an-ip"})) == "not-an-ip"
    a = main.client_key(_req({"X-Forwarded-For": "2001:db8:1:2::1"}))
    b = main.client_key(_req({"X-Forwarded-For": "2001:db8:1:2:ffff::9"}))
    assert a == b == "2001:db8:1:2::/64"


@pytest.mark.asyncio
async def test_new_lookups_are_limited_per_ip(test_client, clean_test_env, clean_priority_state):
    """An IP over its new-lookup limit gets 429; other IPs, cached and queued AIDs are unaffected."""
    import main

    new_q, _ = clean_priority_state
    scraper = {"X-Forwarded-For": "198.51.100.7"}
    other = {"X-Forwarded-For": "203.0.113.5"}

    with patch("main.NEW_LOOKUPS_PER_IP", 3):
        for aid in (101, 102, 103):
            assert test_client.get(f"/anime/{aid}", headers=scraper).status_code == 202

        blocked = test_client.get("/anime/104", headers=scraper)
        assert blocked.status_code == 429
        assert int(blocked.headers["Retry-After"]) > 0
        assert 104 not in main.pending_aids
        assert new_q.qsize() == 3

        # Asking again for an AID that is already queued costs nothing and isn't blocked
        assert test_client.get("/anime/101", headers=scraper).status_code == 202
        # A different caller is unaffected
        assert test_client.get("/anime/104", headers=other).status_code == 202

    assert main.rejected_lookups["rate_limited"] == 1


@pytest.mark.asyncio
async def test_rate_limited_ip_still_gets_cached_data(
    test_client, clean_test_env, clean_priority_state, sample_anime_xml
):
    """The limit only applies to uncached AIDs; cache hits and stale refreshes are unlimited."""
    import aiosqlite

    Path("/tmp/test_anidb/data/1.xml").write_text(sample_anime_xml, encoding="utf-8")
    async with aiosqlite.connect("/tmp/test_anidb/test.db") as db:
        await db.execute(
            "INSERT OR REPLACE INTO anime VALUES (?, ?)", (1, datetime.now().isoformat())
        )
        await db.commit()

    headers = {"X-Forwarded-For": "198.51.100.7"}
    with patch("main.NEW_LOOKUPS_PER_IP", 1):
        assert test_client.get("/anime/201", headers=headers).status_code == 202
        assert test_client.get("/anime/202", headers=headers).status_code == 429
        assert test_client.get("/anime/1", headers=headers).status_code == 200


@pytest.mark.asyncio
async def test_lookup_window_expires(test_client, clean_test_env, clean_priority_state):
    """Lookups older than the window no longer count against the caller."""
    import main

    old = main.time.monotonic() - 7200
    main.new_lookups["198.51.100.7"] = main.deque([old, old, old])
    with patch("main.NEW_LOOKUPS_PER_IP", 3):
        response = test_client.get("/anime/301", headers={"X-Forwarded-For": "198.51.100.7"})
    assert response.status_code == 202


@pytest.mark.asyncio
async def test_out_of_range_aid_is_rejected(test_client, clean_test_env, clean_priority_state):
    """AIDs beyond MAX_AID are never queued or fetched."""
    import main

    new_q, _ = clean_priority_state
    with patch("main.MAX_AID", 50000):
        assert test_client.get("/anime/999999999").status_code == 404
        assert test_client.get("/anime/50001").status_code == 404
        assert test_client.get("/anime/50000").status_code == 202
    assert new_q.qsize() == 1
    assert main.rejected_lookups["out_of_range"] == 2


@pytest.mark.asyncio
async def test_full_queue_rejects_new_aids(test_client, clean_test_env, clean_priority_state):
    """When the new queue is at its cap, further uncached AIDs get 503 with Retry-After."""
    import main

    new_q, _ = clean_priority_state
    with patch("main.MAX_QUEUED_NEW", 2):
        assert test_client.get("/anime/401").status_code == 202
        assert test_client.get("/anime/402").status_code == 202
        full = test_client.get("/anime/403")
    assert full.status_code == 503
    assert full.headers["Retry-After"] == "3600"
    assert new_q.qsize() == 2
    assert main.rejected_lookups["queue_full"] == 1


@pytest.mark.asyncio
async def test_stats_reports_rejections(test_client, clean_test_env, clean_priority_state):
    """/stats shows how many lookups were turned away and why."""
    test_client.get("/anime/999999999")
    data = test_client.get("/stats").json()
    assert data["rejected_new_lookups"] == {
        "out_of_range": 1,
        "rate_limited": 0,
        "daily_limited": 0,
        "queue_full": 0,
    }


@pytest.mark.asyncio
async def test_daily_cap_blocks_after_hourly_limit_would_reset(
    test_client, clean_test_env, clean_priority_state
):
    """The daily cap holds even when each hour's own limit is never reached."""
    import main

    caller = {"X-Forwarded-For": "198.51.100.7"}
    with patch("main.NEW_LOOKUPS_PER_IP_DAILY", 3), patch("main.NEW_LOOKUPS_PER_IP", 50):
        for aid in (501, 502, 503):
            assert test_client.get(f"/anime/{aid}", headers=caller).status_code == 202

        blocked = test_client.get("/anime/504", headers=caller)
        assert blocked.status_code == 429
        # the entry blocking the caller is hours from expiring, not minutes
        assert int(blocked.headers["Retry-After"]) > 3600
        assert 504 not in main.pending_aids

        # another caller still has their own budget
        other = {"X-Forwarded-For": "203.0.113.5"}
        assert test_client.get("/anime/504", headers=other).status_code == 202

    assert main.rejected_lookups["daily_limited"] == 1
    assert main.rejected_lookups["rate_limited"] == 0


@pytest.mark.asyncio
async def test_daily_cap_counts_across_hours(test_client, clean_test_env, clean_priority_state):
    """Lookups from earlier in the day still count toward the cap, but not after 24h."""
    import main

    now = main.time.monotonic()
    caller = {"X-Forwarded-For": "198.51.100.7"}
    with patch("main.NEW_LOOKUPS_PER_IP_DAILY", 3), patch("main.NEW_LOOKUPS_PER_IP", 50):
        # three lookups 5 hours ago: outside the hourly window, inside the daily one
        main.new_lookups["198.51.100.7"] = main.deque([now - 18000] * 3)
        assert test_client.get("/anime/601", headers=caller).status_code == 429

        # the same three lookups 25 hours ago have aged out
        main.new_lookups["198.51.100.7"] = main.deque([now - 90000] * 3)
        assert test_client.get("/anime/601", headers=caller).status_code == 202


@pytest.mark.asyncio
async def test_hourly_and_daily_limits_can_be_disabled_independently(
    test_client, clean_test_env, clean_priority_state
):
    """Setting a limit to 0 turns only that check off."""
    caller = {"X-Forwarded-For": "198.51.100.7"}
    with patch("main.NEW_LOOKUPS_PER_IP", 0), patch("main.NEW_LOOKUPS_PER_IP_DAILY", 2):
        assert test_client.get("/anime/701", headers=caller).status_code == 202
        assert test_client.get("/anime/702", headers=caller).status_code == 202
        assert test_client.get("/anime/703", headers=caller).status_code == 429

    with patch("main.NEW_LOOKUPS_PER_IP", 2), patch("main.NEW_LOOKUPS_PER_IP_DAILY", 0):
        other = {"X-Forwarded-For": "203.0.113.5"}
        assert test_client.get("/anime/711", headers=other).status_code == 202
        assert test_client.get("/anime/712", headers=other).status_code == 202
        assert test_client.get("/anime/713", headers=other).status_code == 429

    with patch("main.NEW_LOOKUPS_PER_IP", 0), patch("main.NEW_LOOKUPS_PER_IP_DAILY", 0):
        third = {"X-Forwarded-For": "192.0.2.44"}
        for aid in range(721, 731):
            assert test_client.get(f"/anime/{aid}", headers=third).status_code == 202


# ============================================================================
# AniDB Error Response Tests
# ============================================================================


def _anidb_replies(text):
    response = MagicMock(text=text)
    response.raise_for_status = MagicMock()
    return AsyncMock(return_value=response)


@pytest.mark.asyncio
async def test_not_found_response_is_returned_for_caching(clean_test_env):
    """A genuine "Anime not found" answer is cached like any other result."""
    from main import fetch_from_anidb

    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = _anidb_replies(
            "<error>Anime not found</error>"
        )
        assert await fetch_from_anidb(14847) == "<error>Anime not found</error>"


@pytest.mark.parametrize(
    "body",
    [
        "<error>client version missing or invalid</error>",
        '<error code="500">Internal server error</error>',
        "  <error>Misc error</error>\n",
    ],
)
@pytest.mark.asyncio
async def test_other_error_responses_are_not_cached(clean_test_env, body):
    """Any other <error> document is a failure: it raises, is logged as failed, and is not stored."""
    import aiosqlite

    from main import fetch_from_anidb

    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = _anidb_replies(body)
        with pytest.raises(HTTPException) as exc_info:
            await fetch_from_anidb(1)

    assert exc_info.value.status_code == 503
    assert "AniDB returned an error" in exc_info.value.detail
    async with aiosqlite.connect("/tmp/test_anidb/test.db") as db:
        cursor = await db.execute("SELECT success FROM api_logs WHERE aid = 1")
        assert await cursor.fetchall() == [(0,)]


def test_not_found_entries_get_a_long_lifetime():
    """Cached not-found answers are rechecked rarely; real anime keep their tiered lifetimes."""
    import main

    assert main.refresh_threshold("<error>Anime not found</error>") == timedelta(days=180)
    assert main.refresh_threshold(_dated_xml("2024-01-01")) == timedelta(days=14)


@pytest.mark.asyncio
async def test_worker_does_not_write_file_for_failed_error_response(
    clean_test_env, clean_priority_state
):
    """When AniDB returns a non-not-found error, nothing is cached and the queue row is dropped."""
    import main

    await main.enqueue_aid(77, is_new=True)
    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.get = _anidb_replies(
            "<error>client version missing or invalid</error>"
        )
        with patch("main.THROTTLE_SECONDS", 0):
            await _run_worker()

    assert not Path("/tmp/test_anidb/data/77.xml").exists()
    assert await _pending_rows() == []
