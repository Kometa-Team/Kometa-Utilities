"""Tests for IMDB service."""

import asyncio
import gzip
import io
import json
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


def _make_tsv_gz(header: str, rows: list[str]) -> bytes:
    """Build a gzip-compressed TSV bytes object for testing."""
    content = "\n".join([header] + rows) + "\n"
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as f:
        f.write(content.encode())
    return buf.getvalue()


def test_create_schema_creates_all_tables():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    try:
        conn = sqlite3.connect(db_path)
        from importer import create_schema

        create_schema(conn)
        conn.commit()
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = {row[0] for row in cursor.fetchall()}
        assert tables == {
            "imdb_parental",
            "title_basics",
            "title_ratings",
            "title_akas",
            "title_crew",
            "title_episode",
            "title_principals",
            "name_basics",
            "import_meta",
        }
        conn.close()
    finally:
        db_path.unlink(missing_ok=True)


def test_create_schema_creates_indexes():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    try:
        conn = sqlite3.connect(db_path)
        from importer import create_schema

        create_schema(conn)
        conn.commit()
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
        )
        indexes = {row[0] for row in cursor.fetchall()}
        assert "idx_tb_type" in indexes
        assert "idx_aka_tconst" in indexes
        assert "idx_pr_nconst" in indexes
        assert "idx_ep_parent" in indexes
        conn.close()
    finally:
        db_path.unlink(missing_ok=True)


def test_import_table_inserts_rows(tmp_path):
    data = _make_tsv_gz(
        "tconst\ttitleType\tprimaryTitle\toriginalTitle\tisAdult\tstartYear\tendYear\truntimeMinutes\tgenres",
        [
            "tt0000001\tshort\tCarmencita\tCarmencita\t0\t1894\t\\N\t1\tDocumentary,Short",
            "tt0000002\tshort\tLe clown\tLe clown\t0\t1892\t\\N\t5\tComedy",
        ],
    )
    gz_path = tmp_path / "title.basics.tsv.gz"
    gz_path.write_bytes(data)

    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    from importer import create_schema, import_table

    create_schema(conn)
    conn.commit()
    count = import_table(
        conn,
        gz_path,
        "title_basics",
        [
            "tconst",
            "titleType",
            "primaryTitle",
            "originalTitle",
            "isAdult",
            "startYear",
            "endYear",
            "runtimeMinutes",
            "genres",
        ],
        min_rows=1,
    )
    assert count == 2
    rows = conn.execute(
        "SELECT tconst, startYear, endYear FROM title_basics ORDER BY tconst"
    ).fetchall()
    assert rows[0] == ("tt0000001", 1894, None)  # \N → NULL
    assert rows[1] == ("tt0000002", 1892, None)
    conn.close()


def test_import_table_raises_on_min_rows_not_met(tmp_path):
    data = _make_tsv_gz(
        "tconst\ttitleType\tprimaryTitle\toriginalTitle\tisAdult\tstartYear\tendYear\truntimeMinutes\tgenres",
        ["tt0000001\tshort\tCarmencita\tCarmencita\t0\t1894\t\\N\t1\tDocumentary,Short"],
    )
    gz_path = tmp_path / "title.basics.tsv.gz"
    gz_path.write_bytes(data)

    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    from importer import create_schema, import_table

    create_schema(conn)
    conn.commit()
    with pytest.raises(ValueError, match="too few rows"):
        import_table(
            conn,
            gz_path,
            "title_basics",
            [
                "tconst",
                "titleType",
                "primaryTitle",
                "originalTitle",
                "isAdult",
                "startYear",
                "endYear",
                "runtimeMinutes",
                "genres",
            ],
            min_rows=1000,
        )
    conn.close()


@pytest.mark.asyncio
async def test_download_datasets_creates_files(tmp_path):
    """download_datasets saves each dataset file to the target directory."""
    fake_gz_content = b"\x1f\x8b\x08\x00\x00\x00\x00\x00"  # gzip magic bytes

    # Build a mock httpx.AsyncClient that streams fake content
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()

    async def fake_aiter_bytes(chunk_size=65536):
        yield fake_gz_content

    mock_resp.aiter_bytes = fake_aiter_bytes

    mock_stream_ctx = AsyncMock()
    mock_stream_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_stream_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_client = AsyncMock()
    mock_client.stream = MagicMock(return_value=mock_stream_ctx)
    mock_head_resp = MagicMock()
    mock_head_resp.status_code = 200
    mock_head_resp.raise_for_status = MagicMock()
    mock_head_resp.headers = {
        "etag": '"abc"',
        "last-modified": "Fri, 04 Apr 2026 00:00:00 GMT",
        "content-length": "8",
    }
    mock_client.head = AsyncMock(return_value=mock_head_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("importer.httpx.AsyncClient", return_value=mock_client):
        from importer import download_datasets

        result, changed_stems = await download_datasets(tmp_path)

    expected_stems = {
        "title.basics",
        "title.ratings",
        "title.akas",
        "title.crew",
        "title.episode",
        "title.principals",
        "name.basics",
    }
    assert len(result) == 7
    assert set(changed_stems) == expected_stems
    assert set(result.keys()) == expected_stems
    for _stem, path in result.items():
        assert path.exists()
        assert path.name.endswith(".tsv.gz")


@pytest.mark.asyncio
async def test_download_datasets_skips_unchanged_files(tmp_path):
    """download_datasets should skip redownloading files whose remote metadata is unchanged."""
    from importer import DATASET_FILES, download_datasets

    for _stem, filename in DATASET_FILES.items():
        (tmp_path / filename).write_bytes(_make_tsv_gz("col", []))

    manifest = {
        stem: {
            "etag": '"same"',
            "last_modified": "Fri, 04 Apr 2026 00:00:00 GMT",
            "content_length": "26",
            "last_checked": "2026-03-20T00:00:00+00:00",
            "last_downloaded": "2026-03-20T00:00:00+00:00",
        }
        for stem in DATASET_FILES
    }
    (tmp_path / "dataset_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    mock_client = AsyncMock()
    mock_head_resp = MagicMock()
    mock_head_resp.status_code = 200
    mock_head_resp.raise_for_status = MagicMock()
    mock_head_resp.headers = {
        "etag": '"same"',
        "last-modified": "Fri, 04 Apr 2026 00:00:00 GMT",
        "content-length": "26",
    }
    mock_client.head = AsyncMock(return_value=mock_head_resp)
    mock_client.stream = MagicMock(side_effect=AssertionError("download should not occur"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("importer.httpx.AsyncClient", return_value=mock_client):
        result, changed_stems = await download_datasets(tmp_path)

    assert len(result) == 7
    assert changed_stems == []


def _make_all_gz_files(tmp_path):
    """Create minimal valid TSV.gz files for all 7 datasets."""
    basics_data = _make_tsv_gz(
        "tconst\ttitleType\tprimaryTitle\toriginalTitle\tisAdult\tstartYear\tendYear\truntimeMinutes\tgenres",
        [f"tt{i:07d}\tmovie\tTitle {i}\tTitle {i}\t0\t2000\t\\N\t90\tAction" for i in range(1, 6)],
    )
    ratings_data = _make_tsv_gz(
        "tconst\taverageRating\tnumVotes",
        [f"tt{i:07d}\t{7.0 + i * 0.1:.1f}\t{50000 + i * 1000}" for i in range(1, 6)],
    )
    akas_data = _make_tsv_gz(
        "titleId\tordering\ttitle\tregion\tlanguage\ttypes\tattributes\tisOriginalTitle",
        [f"tt{i:07d}\t1\tTitle {i}\tUS\ten\t\\N\t\\N\t1" for i in range(1, 6)],
    )
    crew_data = _make_tsv_gz(
        "tconst\tdirectors\twriters",
        [f"tt{i:07d}\tnm{i:07d}\t\\N" for i in range(1, 6)],
    )
    episode_data = _make_tsv_gz(
        "tconst\tparentTconst\tseasonNumber\tepisodeNumber",
        [],
    )
    principals_data = _make_tsv_gz(
        "tconst\tordering\tnconst\tcategory\tjob\tcharacters",
        [f"tt{i:07d}\t1\tnm{i:07d}\tactor\t\\N\t\\N" for i in range(1, 6)],
    )
    names_data = _make_tsv_gz(
        "nconst\tprimaryName\tbirthYear\tdeathYear\tprimaryProfession\tknownForTitles",
        [f"nm{i:07d}\tPerson {i}\t1970\t\\N\tactor\ttt{i:07d}" for i in range(1, 6)],
    )

    gz_dir = tmp_path / "gz"
    gz_dir.mkdir()
    files = {
        "title.basics": basics_data,
        "title.ratings": ratings_data,
        "title.akas": akas_data,
        "title.crew": crew_data,
        "title.episode": episode_data,
        "title.principals": principals_data,
        "name.basics": names_data,
    }
    gz_paths = {}
    for stem, data in files.items():
        path = gz_dir / f"{stem}.tsv.gz"
        path.write_bytes(data)
        gz_paths[stem] = path
    return gz_paths


def test_run_direct_import_produces_populated_db(tmp_path):
    gz_paths = _make_all_gz_files(tmp_path)
    live_db = tmp_path / "imdb.db"

    from importer import run_direct_import

    run_direct_import(gz_paths, live_db, min_rows_override=0)

    assert live_db.exists()
    conn = sqlite3.connect(live_db)
    count = conn.execute("SELECT COUNT(*) FROM title_basics").fetchone()[0]
    assert count == 5
    # Verify import_meta has last_refresh
    row = conn.execute("SELECT value FROM import_meta WHERE key='last_refresh'").fetchone()
    assert row is not None
    conn.close()


def test_run_direct_import_updates_only_changed_tables(tmp_path):
    gz_paths = _make_all_gz_files(tmp_path)
    live_db = tmp_path / "imdb.db"

    from importer import run_direct_import

    run_direct_import(gz_paths, live_db, min_rows_override=0)

    updated_ratings = _make_tsv_gz(
        "tconst\taverageRating\tnumVotes",
        [
            "tt0000001\t9.9\t999999",
            "tt0000002\t8.2\t111111",
            "tt0000003\t7.1\t222222",
            "tt0000004\t6.0\t333333",
            "tt0000005\t5.0\t444444",
        ],
    )
    ratings_path = tmp_path / "gz" / "title.ratings.tsv.gz"
    ratings_path.write_bytes(updated_ratings)
    run_direct_import(gz_paths, live_db, changed_stems=["title.ratings"], min_rows_override=0)

    conn = sqlite3.connect(live_db)
    rating_row = conn.execute(
        "SELECT averageRating, numVotes FROM title_ratings WHERE tconst = 'tt0000001'"
    ).fetchone()
    basics_row = conn.execute(
        "SELECT primaryTitle, genres FROM title_basics WHERE tconst = 'tt0000001'"
    ).fetchone()
    conn.close()

    assert rating_row == (9.9, 999999)
    assert basics_row == ("Title 1", "Action")


def test_run_direct_import_leaves_live_db_on_failure(tmp_path):
    """If import fails, the original live DB is untouched (WAL rollback)."""
    live_db = tmp_path / "imdb.db"
    # Create a "live" DB with known content
    conn = sqlite3.connect(live_db)
    conn.execute("CREATE TABLE sentinel (val TEXT)")
    conn.execute("INSERT INTO sentinel VALUES ('original')")
    conn.commit()
    conn.close()

    # Provide an invalid (not-gzip) file to trigger failure
    gz_dir = tmp_path / "gz"
    gz_dir.mkdir()
    bad_gz = gz_dir / "title.basics.tsv.gz"
    bad_gz.write_bytes(b"not valid gzip content")
    gz_paths = {"title.basics": bad_gz}

    from importer import run_direct_import

    with pytest.raises(Exception, match="."):  # noqa: B017
        run_direct_import(gz_paths, live_db, min_rows_override=0)

    # Live DB must still be the original
    conn = sqlite3.connect(live_db)
    val = conn.execute("SELECT val FROM sentinel").fetchone()[0]
    assert val == "original"
    conn.close()


def _seed_db_for_charts(db_path):
    """Seed a test DB with data for chart tests.

    Movies are chosen so that Bayesian weighted-rating (wr) order diverges from
    raw averageRating order, which lets the sort tests verify wr behaviour:

      Alpha:  rating=9.0, votes=25001  (barely above threshold → pulled toward mean)
      Beta:   rating=8.0, votes=1000000 (huge vote count → wr ≈ raw rating)
      Gamma:  rating=2.0, votes=100000

    With min_votes=25000 all three qualify.  C (mean) = (9+8+2)/3 ≈ 6.33.
      wr(Alpha) ≈ 7.67  ← penalised despite highest raw rating
      wr(Beta)  ≈ 7.96  ← boosted despite lower raw rating
      wr(Gamma) ≈ 2.87

    Descending wr order:  Beta > Alpha > Gamma  (raw rating order: Alpha > Beta > Gamma)
    Ascending  wr order:  Gamma < Alpha < Beta   (raw rating order: Gamma < Beta < Alpha)
    """
    conn = sqlite3.connect(db_path)
    from importer import create_schema

    create_schema(conn)
    titles = [
        ("tt0000001", "movie", "Alpha", "Alpha", 0, 2000, None, 120, "Action"),
        ("tt0000002", "movie", "Beta", "Beta", 0, 2001, None, 90, "Drama"),
        ("tt0000003", "movie", "Gamma", "Gamma", 0, 2002, None, 100, "Comedy"),
        ("tt0000004", "tvSeries", "Delta", "Delta", 0, 2003, 2005, None, "Drama"),
        ("tt0000005", "tvSeries", "Epsilon", "Epsilon", 0, 2004, None, None, "Action"),
    ]
    conn.executemany("INSERT INTO title_basics VALUES (?,?,?,?,?,?,?,?,?)", titles)
    ratings = [
        ("tt0000001", 9.0, 25001),  # Alpha: high raw rating, barely-qualifying votes
        ("tt0000002", 8.0, 1000000),  # Beta:  lower raw rating, massive vote count
        ("tt0000003", 2.0, 100000),  # Gamma: low rating, moderate votes
        ("tt0000004", 9.0, 50000),
        ("tt0000005", 8.0, 60000),
    ]
    conn.executemany("INSERT INTO title_ratings VALUES (?,?,?)", ratings)
    # English akas for tt0000001 and tt0000002 only
    conn.execute("INSERT INTO title_akas VALUES ('tt0000001',1,'Alpha','US','en',NULL,NULL,1)")
    conn.execute("INSERT INTO title_akas VALUES ('tt0000002',1,'Beta','US','en',NULL,NULL,1)")
    conn.commit()
    conn.close()


def _compute_wr(rating: float, votes: int, mean_rating: float, min_votes: int) -> float:
    """Replicate the Bayesian weighted-rating formula used by charts._compute_chart."""
    return (votes / (votes + min_votes)) * rating + (min_votes / (votes + min_votes)) * mean_rating


def test_rebuild_all_charts_populates_cache(tmp_path):
    db_path = tmp_path / "imdb.db"
    _seed_db_for_charts(db_path)
    import charts

    charts.chart_cache = {}
    charts.rebuild_all_charts(db_path, min_votes=25000)
    assert "top_movies" in charts.chart_cache
    assert "top_shows" in charts.chart_cache
    assert len(charts.chart_cache["top_movies"]) <= 3  # 3 movies with votes >= 25000
    assert len(charts.chart_cache["top_shows"]) <= 2  # 2 tvSeries with votes >= 25000


def test_top_movies_chart_sorted_by_weighted_rating(tmp_path):
    """Chart must be ordered by Bayesian wr, not raw averageRating.

    Seed data is deliberately chosen so that wr order diverges from raw-rating
    order: Beta (8.0, 1 000 000 votes) outscores Alpha (9.0, 25 001 votes)
    because Alpha's rating is heavily pulled toward the mean.
    """
    db_path = tmp_path / "imdb.db"
    _seed_db_for_charts(db_path)
    import charts

    charts.chart_cache = {}
    min_votes = 25000
    charts.rebuild_all_charts(db_path, min_votes=min_votes)
    top = charts.chart_cache["top_movies"]

    # Compute expected wr scores for the qualifying movies.
    # mean_rating = (9.0 + 8.0 + 2.0) / 3
    mean_rating = (9.0 + 8.0 + 2.0) / 3
    wr_scores = [
        _compute_wr(item["averageRating"], item["numVotes"], mean_rating, min_votes) for item in top
    ]
    # Chart must be in descending wr order.
    assert wr_scores == sorted(wr_scores, reverse=True)
    # Sanity: Beta (8.0, 1 000 000) should rank above Alpha (9.0, 25 001).
    titles = [item["primaryTitle"] for item in top]
    assert titles.index("Beta") < titles.index("Alpha")


def test_lowest_rated_chart_is_ascending(tmp_path):
    """lowest_rated chart must be ordered by ascending Bayesian wr.

    With the seed data the ascending wr order is Gamma < Alpha < Beta, which
    differs from the raw-rating ascending order (Gamma < Beta < Alpha).
    """
    db_path = tmp_path / "imdb.db"
    _seed_db_for_charts(db_path)
    import charts

    charts.chart_cache = {}
    min_votes = 25000
    charts.rebuild_all_charts(db_path, min_votes=min_votes)
    bottom = charts.chart_cache["lowest_rated"]

    # Compute expected wr scores and verify ascending order.
    mean_rating = (9.0 + 8.0 + 2.0) / 3
    wr_scores = [
        _compute_wr(item["averageRating"], item["numVotes"], mean_rating, min_votes)
        for item in bottom
    ]
    assert wr_scores == sorted(wr_scores)
    # Sanity: Alpha (9.0, 25 001) should rank below Beta (8.0, 1 000 000) in
    # ascending order because Alpha is penalised by its low vote count.
    titles = [item["primaryTitle"] for item in bottom]
    assert titles.index("Alpha") < titles.index("Beta")


def test_top_english_only_includes_english_titles(tmp_path):
    db_path = tmp_path / "imdb.db"
    _seed_db_for_charts(db_path)
    import charts

    charts.chart_cache = {}
    charts.rebuild_all_charts(db_path, min_votes=25000)
    english = charts.chart_cache["top_english"]
    tconsts = {item["tconst"] for item in english}
    assert "tt0000001" in tconsts
    assert "tt0000002" in tconsts
    assert "tt0000003" not in tconsts  # no en aka


def test_chart_items_have_required_fields(tmp_path):
    db_path = tmp_path / "imdb.db"
    _seed_db_for_charts(db_path)
    import charts

    charts.chart_cache = {}
    charts.rebuild_all_charts(db_path, min_votes=25000)
    item = charts.chart_cache["top_movies"][0]
    for field in ("tconst", "primaryTitle", "startYear", "averageRating", "numVotes", "rank"):
        assert field in item, f"Missing field: {field}"


def test_chart_cache_replaced_atomically(tmp_path):
    db_path = tmp_path / "imdb.db"
    _seed_db_for_charts(db_path)
    import charts

    charts.chart_cache = {"stale_key": [{"tconst": "ttOLD"}]}
    charts.rebuild_all_charts(db_path, min_votes=25000)
    assert "stale_key" not in charts.chart_cache
    assert "top_movies" in charts.chart_cache


def test_stats_returns_initializing_when_no_db(tmp_path, monkeypatch):
    import main

    monkeypatch.setattr(main, "DB_PATH", tmp_path / "nonexistent.db")
    monkeypatch.setattr(main, "last_refresh", None)
    monkeypatch.setattr(main, "current_phase", "idle")
    monkeypatch.setattr(main, "download_progress", {})
    monkeypatch.setattr(main, "import_progress", {})
    monkeypatch.setattr(main, "last_activity", None)
    client = TestClient(main.app, raise_server_exceptions=False)
    response = client.get("/stats")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "initializing"
    assert "phase" in data
    assert "download_progress" in data
    assert "import_progress" in data
    assert "last_activity" in data
    assert "parental_cache" not in data


def test_stats_returns_online_with_db(tmp_path, monkeypatch):
    db_path = tmp_path / "imdb.db"
    conn = sqlite3.connect(db_path)
    import json

    from importer import create_schema

    row_counts = {
        "title_basics": 100,
        "title_ratings": 50,
        "title_akas": 200,
        "title_crew": 100,
        "title_episode": 30,
        "title_principals": 150,
        "name_basics": 80,
    }
    create_schema(conn)
    conn.execute("INSERT INTO import_meta VALUES ('last_refresh', '2026-03-24T03:00:00+00:00')")
    conn.execute("INSERT INTO import_meta VALUES ('row_counts', ?)", (json.dumps(row_counts),))
    conn.commit()
    conn.close()

    import main

    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(main, "last_refresh", "2026-03-24T03:00:00+00:00")
    monkeypatch.setattr(main, "current_phase", "idle")
    monkeypatch.setattr(main, "last_activity", None)
    client = TestClient(main.app)
    response = client.get("/stats")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "online"
    assert data["phase"] == "idle"
    assert "last_activity" in data
    assert "last_refresh" in data
    assert data["last_refresh"] == "2026-03-24T03:00:00+00:00"
    for table in row_counts:
        assert table in data["table_counts"], f"Missing table count: {table}"
        assert data["table_counts"][table] == row_counts[table]
    assert data["parental_cache"]["items_cached"] == 0
    assert data["parental_cache"]["flag_counts"] == {
        "nudity": 0,
        "violence": 0,
        "profanity": 0,
        "alcohol": 0,
        "frightening": 0,
    }
    assert "charts_cached" in data


def test_stats_returns_parental_cache_item_details(tmp_path, monkeypatch):
    db_path = tmp_path / "imdb.db"
    _seed_full_test_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO imdb_parental(imdb_id, nudity, violence, profanity, alcohol, frightening, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "tt0111161",
            "Mild",
            "Moderate",
            "None",
            "Mild",
            "Severe",
            "2026-04-04T00:00:00+00:00",
        ),
    )
    conn.execute(
        """
        INSERT INTO imdb_parental(imdb_id, nudity, violence, profanity, alcohol, frightening, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "tt0096697",
            "None",
            "",
            "Mild",
            None,
            "Moderate",
            "2026-04-04T00:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()

    import main

    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(main, "last_refresh", "2026-03-24T03:00:00+00:00")
    monkeypatch.setattr(main, "current_phase", "idle")
    monkeypatch.setattr(main, "last_activity", None)
    client = TestClient(main.app)
    response = client.get("/stats")
    assert response.status_code == 200
    data = response.json()
    assert data["parental_cache"] == {
        "items_cached": 2,
        "flag_counts": {
            "nudity": 2,
            "violence": 1,
            "profanity": 2,
            "alcohol": 1,
            "frightening": 2,
        },
    }


def test_root_returns_dashboard(tmp_path, monkeypatch):
    import main

    monkeypatch.setattr(main, "DB_PATH", tmp_path / "nonexistent.db")
    client = TestClient(main.app, raise_server_exceptions=False)
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "IMDB Service Dashboard" in response.text


def test_endpoints_returns_html(tmp_path, monkeypatch):
    import main

    monkeypatch.setattr(main, "DB_PATH", tmp_path / "nonexistent.db")
    client = TestClient(main.app, raise_server_exceptions=False)
    response = client.get("/endpoints")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "IMDB" in response.text


def _seed_full_test_db(db_path):
    """Seed a test DB with a complete set of test data for endpoint tests."""
    conn = sqlite3.connect(db_path)
    from importer import create_schema

    create_schema(conn)
    conn.execute(
        "INSERT INTO title_basics VALUES ('tt0111161','movie','The Shawshank Redemption','The Shawshank Redemption',0,1994,NULL,142,'Drama,Crime,Thriller')"
    )
    conn.execute(
        "INSERT INTO title_basics VALUES ('tt0096697','tvSeries','The Simpsons','The Simpsons',0,1989,NULL,22,'Animation,Comedy,Family')"
    )
    conn.execute(
        "INSERT INTO title_basics VALUES ('tt0502973','tvEpisode','Simpsons Roasting on an Open Fire','Simpsons Roasting on an Open Fire',0,1989,NULL,23,'Animation,Comedy,Family')"
    )
    conn.execute(
        "INSERT INTO title_basics VALUES ('tt9000001','movie','Adult Example','Adult Example',1,1999,NULL,95,'Drama,Thriller,Romance')"
    )
    conn.execute("INSERT INTO title_ratings VALUES ('tt0111161', 9.3, 2800000)")
    conn.execute("INSERT INTO title_ratings VALUES ('tt0096697', 8.0, 500000)")
    conn.execute("INSERT INTO title_ratings VALUES ('tt0502973', 8.2, 12000)")
    conn.execute("INSERT INTO title_ratings VALUES ('tt9000001', 4.4, 800)")
    conn.execute("INSERT INTO title_crew VALUES ('tt0111161','nm0001104',NULL)")
    conn.execute(
        "INSERT INTO title_principals VALUES ('tt0111161',1,'nm0000209','actor',NULL,'[\"Andy Dufresne\"]')"
    )
    conn.execute(
        "INSERT INTO title_principals VALUES ('tt0111161',2,'nm0000151','actor',NULL,'[\"Ellis Boyd Redding\"]')"
    )
    conn.execute("INSERT INTO title_episode VALUES ('tt0502973','tt0096697',1,1)")
    conn.execute(
        "INSERT INTO name_basics VALUES ('nm0001104','Frank Darabont',1959,NULL,'director,writer,producer','tt0111161')"
    )
    conn.commit()
    conn.close()


def _seed_legacy_test_db_without_parental(db_path):
    """Seed an older DB shape that predates the imdb_parental table."""
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE import_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE title_basics (
            tconst TEXT PRIMARY KEY,
            titleType TEXT,
            primaryTitle TEXT,
            originalTitle TEXT,
            isAdult INTEGER,
            startYear INTEGER,
            endYear INTEGER,
            runtimeMinutes INTEGER,
            genres TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO title_basics VALUES ('tt0111161','movie','The Shawshank Redemption','The Shawshank Redemption',0,1994,NULL,142,'Drama,Crime,Thriller')"
    )
    conn.commit()
    conn.close()


def test_get_title_returns_full_record(tmp_path, monkeypatch):
    db_path = tmp_path / "imdb.db"
    _seed_full_test_db(db_path)
    import main

    monkeypatch.setattr(main, "DB_PATH", db_path)
    client = TestClient(main.app)
    response = client.get("/title/tt0111161")
    assert response.status_code == 200
    data = response.json()
    assert data["tconst"] == "tt0111161"
    assert data["primaryTitle"] == "The Shawshank Redemption"
    assert data["averageRating"] == 9.3
    assert data["numVotes"] == 2800000
    assert data["directors"] == "nm0001104"
    assert len(data["principals"]) == 2
    assert data["principals"][0]["ordering"] == 1
    assert data["principals"][1]["ordering"] == 2


def test_get_title_includes_episode_count_for_series(tmp_path, monkeypatch):
    db_path = tmp_path / "imdb.db"
    _seed_full_test_db(db_path)
    import main

    monkeypatch.setattr(main, "DB_PATH", db_path)
    client = TestClient(main.app)
    response = client.get("/title/tt0096697")
    assert response.status_code == 200
    data = response.json()
    assert data["episode_count"] == 1


def test_get_title_movie_has_no_episode_count(tmp_path, monkeypatch):
    db_path = tmp_path / "imdb.db"
    _seed_full_test_db(db_path)
    import main

    monkeypatch.setattr(main, "DB_PATH", db_path)
    client = TestClient(main.app)
    response = client.get("/title/tt0111161")  # movie, not a series
    assert response.status_code == 200
    assert "episode_count" not in response.json()


def test_get_title_returns_404_for_unknown(tmp_path, monkeypatch):
    db_path = tmp_path / "imdb.db"
    _seed_full_test_db(db_path)
    import main

    monkeypatch.setattr(main, "DB_PATH", db_path)
    client = TestClient(main.app)
    response = client.get("/title/tt9999999")
    assert response.status_code == 404


def test_get_title_returns_503_when_no_db(tmp_path, monkeypatch):
    import main

    monkeypatch.setattr(main, "DB_PATH", tmp_path / "nonexistent.db")
    client = TestClient(main.app, raise_server_exceptions=False)
    response = client.get("/title/tt0111161")
    assert response.status_code == 503


def test_get_person_returns_record(tmp_path, monkeypatch):
    db_path = tmp_path / "imdb.db"
    _seed_full_test_db(db_path)
    import main

    monkeypatch.setattr(main, "DB_PATH", db_path)
    client = TestClient(main.app)
    response = client.get("/person/nm0001104")
    assert response.status_code == 200
    data = response.json()
    assert data["nconst"] == "nm0001104"
    assert data["primaryName"] == "Frank Darabont"
    assert data["birthYear"] == 1959


def test_get_person_returns_404_for_unknown(tmp_path, monkeypatch):
    db_path = tmp_path / "imdb.db"
    _seed_full_test_db(db_path)
    import main

    monkeypatch.setattr(main, "DB_PATH", db_path)
    client = TestClient(main.app)
    response = client.get("/person/nm9999999")
    assert response.status_code == 404


def test_get_person_returns_503_when_no_db(tmp_path, monkeypatch):
    import main

    monkeypatch.setattr(main, "DB_PATH", tmp_path / "nonexistent.db")
    client = TestClient(main.app, raise_server_exceptions=False)
    response = client.get("/person/nm0001104")
    assert response.status_code == 503


def test_ratings_returns_title_ratings(tmp_path, monkeypatch):
    db_path = tmp_path / "imdb.db"
    _seed_full_test_db(db_path)
    import main

    monkeypatch.setattr(main, "DB_PATH", db_path)
    client = TestClient(main.app)
    response = client.get("/ratings/tt0111161")
    assert response.status_code == 200
    data = response.json()
    assert data["field"] == "ratings"
    assert data["imdb_id"] == "tt0111161"
    assert data["result"]["tconst"] == "tt0111161"
    assert data["result"]["averageRating"] == 9.3
    assert data["result"]["numVotes"] == 2800000
    assert data["result"]["titleType"] == "movie"


def test_ratings_returns_episode_ratings(tmp_path, monkeypatch):
    db_path = tmp_path / "imdb.db"
    _seed_full_test_db(db_path)
    import main

    monkeypatch.setattr(main, "DB_PATH", db_path)
    client = TestClient(main.app)
    response = client.get("/ratings/tt0502973")
    assert response.status_code == 200
    data = response.json()
    assert data["result"]["tconst"] == "tt0502973"
    assert data["result"]["titleType"] == "tvEpisode"
    assert data["result"]["averageRating"] == 8.2


def test_genre_returns_title_genres(tmp_path, monkeypatch):
    db_path = tmp_path / "imdb.db"
    _seed_full_test_db(db_path)
    import main

    monkeypatch.setattr(main, "DB_PATH", db_path)
    client = TestClient(main.app)
    response = client.get("/genre/tt0096697")
    assert response.status_code == 200
    data = response.json()
    assert data["imdb_id"] == "tt0096697"
    assert data["result"]["tconst"] == "tt0096697"
    assert data["result"]["genres"] == ["Animation", "Comedy", "Family"]


def test_ratings_returns_404_for_unknown_title(tmp_path, monkeypatch):
    db_path = tmp_path / "imdb.db"
    _seed_full_test_db(db_path)
    import main

    monkeypatch.setattr(main, "DB_PATH", db_path)
    client = TestClient(main.app)
    response = client.get("/ratings/tt9999999")
    assert response.status_code == 404


def test_genre_returns_404_for_unknown_title(tmp_path, monkeypatch):
    db_path = tmp_path / "imdb.db"
    _seed_full_test_db(db_path)
    import main

    monkeypatch.setattr(main, "DB_PATH", db_path)
    client = TestClient(main.app)
    response = client.get("/genre/tt9999999")
    assert response.status_code == 404


def test_ratings_returns_503_when_no_db(tmp_path, monkeypatch):
    import main

    monkeypatch.setattr(main, "DB_PATH", tmp_path / "nonexistent.db")
    client = TestClient(main.app, raise_server_exceptions=False)
    response = client.get("/ratings/tt0111161")
    assert response.status_code == 503


def test_genre_returns_503_when_no_db(tmp_path, monkeypatch):
    import main

    monkeypatch.setattr(main, "DB_PATH", tmp_path / "nonexistent.db")
    client = TestClient(main.app, raise_server_exceptions=False)
    response = client.get("/genre/tt0111161")
    assert response.status_code == 503


def test_parental_endpoint_uses_cached_value_when_fresh(tmp_path, monkeypatch):
    db_path = tmp_path / "imdb.db"
    _seed_full_test_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO imdb_parental(imdb_id, nudity, violence, profanity, alcohol, frightening, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "tt0111161",
            "Mild",
            "Moderate",
            "None",
            "Mild",
            "Severe",
            "2026-04-04T00:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()

    import main

    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(main, "PARENTAL_GUIDE_TTL_DAYS", 30)

    async def fail_fetch(_imdb_id):
        raise AssertionError("network fetch should not occur for fresh cache")

    monkeypatch.setattr(main, "_fetch_parental_guide_html", fail_fetch)
    client = TestClient(main.app)
    response = client.get("/parental/tt0111161")
    assert response.status_code == 200
    data = response.json()
    assert data["cached"] is True
    assert data["parental_guide"]["Nudity"] == "Mild"
    assert data["parental_guide"]["Frightening"] == "Severe"


def test_parental_endpoint_fetches_and_caches_when_missing(tmp_path, monkeypatch):
    db_path = tmp_path / "imdb.db"
    _seed_full_test_db(db_path)
    import main

    monkeypatch.setattr(main, "DB_PATH", db_path)
    sample_html = """
    <html><body>
      <li class="ipc-metadata-list__item ipc-metadata-list-item--link">
        <a>Sex &amp; Nudity:</a><div><div><div>Mild</div></div></div>
      </li>
      <li class="ipc-metadata-list__item ipc-metadata-list-item--link">
        <a>Violence &amp; Gore:</a><div><div><div>Moderate</div></div></div>
      </li>
      <li class="ipc-metadata-list__item ipc-metadata-list-item--link">
        <a>Profanity:</a><div><div><div>Severe</div></div></div>
      </li>
      <li class="ipc-metadata-list__item ipc-metadata-list-item--link">
        <a>Alcohol, Drugs &amp; Smoking:</a><div><div><div>None</div></div></div>
      </li>
      <li class="ipc-metadata-list__item ipc-metadata-list-item--link">
        <a>Frightening &amp; Intense Scenes:</a><div><div><div>Mild</div></div></div>
      </li>
    </body></html>
    """

    async def fake_fetch(_imdb_id):
        return sample_html

    monkeypatch.setattr(main, "_fetch_parental_guide_html", fake_fetch)
    client = TestClient(main.app)
    response = client.get("/parental/tt0111161")
    assert response.status_code == 200
    data = response.json()
    assert data["cached"] is False
    assert data["parental_guide"] == {
        "Nudity": "Mild",
        "Violence": "Moderate",
        "Profanity": "Severe",
        "Alcohol": "None",
        "Frightening": "Mild",
    }

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT nudity, violence, profanity, alcohol, frightening FROM imdb_parental WHERE imdb_id = ?",
        ("tt0111161",),
    ).fetchone()
    conn.close()
    assert row == ("Mild", "Moderate", "Severe", "None", "Mild")


def test_parental_endpoint_creates_missing_parental_table_on_existing_db(tmp_path, monkeypatch):
    db_path = tmp_path / "imdb.db"
    _seed_legacy_test_db_without_parental(db_path)
    import main

    monkeypatch.setattr(main, "DB_PATH", db_path)
    sample_html = """
    <li class="ipc-metadata-list-item--link">
      <a>Sex &amp; Nudity:</a><div><div><div>Mild</div></div></div>
    </li>
    """

    async def fake_fetch(_imdb_id):
        return sample_html

    monkeypatch.setattr(main, "_fetch_parental_guide_html", fake_fetch)
    client = TestClient(main.app)
    response = client.get("/parental/tt0111161")
    assert response.status_code == 200
    assert response.json()["parental_guide"]["Nudity"] == "Mild"

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT nudity FROM imdb_parental WHERE imdb_id = ?",
        ("tt0111161",),
    ).fetchone()
    conn.close()
    assert row == ("Mild",)


def test_parental_endpoint_ignore_cache_forces_refresh(tmp_path, monkeypatch):
    db_path = tmp_path / "imdb.db"
    _seed_full_test_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO imdb_parental(imdb_id, nudity, violence, profanity, alcohol, frightening, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "tt0111161",
            "None",
            "None",
            "None",
            "None",
            "None",
            "2026-04-04T00:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()

    import main

    monkeypatch.setattr(main, "DB_PATH", db_path)
    sample_html = """
    <li class="ipc-metadata-list-item--link">
      <a>Sex &amp; Nudity:</a><div><div><div>Severe</div></div></div>
    </li>
    """

    async def fake_fetch(_imdb_id):
        return sample_html

    monkeypatch.setattr(main, "_fetch_parental_guide_html", fake_fetch)
    client = TestClient(main.app)
    response = client.get("/parental/tt0111161?ignore_cache=true")
    assert response.status_code == 200
    data = response.json()
    assert data["cached"] is False
    assert data["parental_guide"]["Nudity"] == "Severe"
    assert data["parental_guide"]["Violence"] == "None"


def test_parental_endpoint_returns_404_when_page_has_no_categories(tmp_path, monkeypatch):
    db_path = tmp_path / "imdb.db"
    _seed_full_test_db(db_path)
    import main

    monkeypatch.setattr(main, "DB_PATH", db_path)

    async def fake_fetch(_imdb_id):
        return "<html><body>No parental guide</body></html>"

    monkeypatch.setattr(main, "_fetch_parental_guide_html", fake_fetch)
    client = TestClient(main.app, raise_server_exceptions=False)
    response = client.get("/parental/tt0111161")
    assert response.status_code == 404


def test_parental_endpoint_returns_404_when_page_has_no_guide_notice(tmp_path, monkeypatch):
    db_path = tmp_path / "imdb.db"
    _seed_full_test_db(db_path)
    import main

    monkeypatch.setattr(main, "DB_PATH", db_path)

    async def fake_fetch(_imdb_id):
        return """
        <article class="sc-a750b45a-0 dawkdM">
          <p>It looks like we don't have a parents guide for this title yet.
          <button>Be the first to contribute.</button></p>
          <a href="https://contribute.imdb.com/updates/guide/parental_guide?ref_=ttpg_emt_hlp">
            Learn more
          </a>
        </article>
        """

    monkeypatch.setattr(main, "_fetch_parental_guide_html", fake_fetch)
    client = TestClient(main.app, raise_server_exceptions=False)
    response = client.get("/parental/tt0111161")
    assert response.status_code == 404


def test_parental_endpoint_returns_503_when_no_db(tmp_path, monkeypatch):
    import main

    monkeypatch.setattr(main, "DB_PATH", tmp_path / "nonexistent.db")
    client = TestClient(main.app, raise_server_exceptions=False)
    response = client.get("/parental/tt0111161")
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_fetch_parental_html_falls_back_to_browser_on_empty_http(monkeypatch):
    import main

    sample_html = """
    <li class="ipc-metadata-list-item--link">
      <a>Sex &amp; Nudity:</a><div><div><div>Mild</div></div></div>
    </li>
    """

    async def fake_http(_imdb_id, proxy_url=None):
        return "<html><body>Accepted</body></html>"

    async def fake_browser(_imdb_id, proxy_url=None):
        return sample_html

    monkeypatch.setattr(main, "_fetch_parental_guide_html_via_http", fake_http)
    monkeypatch.setattr(main, "_fetch_parental_guide_html_via_browser", fake_browser)

    html = await main._fetch_parental_guide_html("tt0111161")
    assert "Sex &amp; Nudity" in html


@pytest.mark.asyncio
async def test_fetch_parental_html_uses_http_when_markers_present(monkeypatch):
    import main

    sample_html = """
    <li class="ipc-metadata-list-item--link">
      <a>Violence &amp; Gore:</a><div><div><div>Moderate</div></div></div>
    </li>
    """

    async def fake_http(_imdb_id, proxy_url=None):
        return sample_html

    async def fail_browser(_imdb_id, proxy_url=None):
        raise AssertionError("browser fallback should not run")

    monkeypatch.setattr(main, "_fetch_parental_guide_html_via_http", fake_http)
    monkeypatch.setattr(main, "_fetch_parental_guide_html_via_browser", fail_browser)

    html = await main._fetch_parental_guide_html("tt0111161")
    assert "Violence &amp; Gore" in html


@pytest.mark.asyncio
async def test_fetch_parental_html_does_not_fallback_on_404(monkeypatch):
    from fastapi import HTTPException

    import main

    async def fake_http(_imdb_id, proxy_url=None):
        raise HTTPException(status_code=404, detail="not found")

    async def fail_browser(_imdb_id, proxy_url=None):
        raise AssertionError("browser fallback should not run on 404")

    monkeypatch.setattr(main, "_fetch_parental_guide_html_via_http", fake_http)
    monkeypatch.setattr(main, "_fetch_parental_guide_html_via_browser", fail_browser)

    with pytest.raises(HTTPException, match="not found"):
        await main._fetch_parental_guide_html("tt9999999")


def test_html_has_waf_challenge_detects_imdb_interstitial():
    import main

    html = """
    <html><body>
    <script>window.AwsWafIntegration = {};</script>
    <noscript>In order to continue, we need to verify that you're not a robot.</noscript>
    </body></html>
    """

    assert main._html_has_waf_challenge(html) is True


def test_html_has_waf_challenge_ignores_real_parental_page():
    import main

    html = """
    <html><body>
      <li class="ipc-metadata-list-item--link">
        <a>Sex &amp; Nudity:</a><div><div><div>Mild</div></div></div>
      </li>
    </body></html>
    """

    assert main._html_has_waf_challenge(html) is False


def test_html_has_no_parental_guide_notice_detects_empty_notice():
    import main

    html = """
    <article class="sc-a750b45a-0 dawkdM">
      <p>It looks like we don't have a parents guide for this title yet.
      <button>Be the first to contribute.</button></p>
      <a href="https://contribute.imdb.com/updates/guide/parental_guide?ref_=ttpg_emt_hlp">
        Learn more
      </a>
    </article>
    """

    assert main._html_has_no_parental_guide_notice(html) is True


def test_choose_parental_proxy_skips_cooled_down_entries(monkeypatch):
    import main

    monkeypatch.setattr(main, "PARENTAL_PROXY_ENABLED", True)
    monkeypatch.setattr(main, "PARENTAL_PROXY_URLS", ["http://proxy-a", "http://proxy-b"])
    monkeypatch.setattr(
        main,
        "proxy_health",
        {"http://proxy-a": datetime.now(timezone.utc) + timedelta(minutes=10)},
    )

    proxy = main._choose_parental_proxy()
    assert proxy == "http://proxy-b"


def test_playwright_proxy_settings_uses_decodo_sticky_browser_session(monkeypatch):
    import main

    monkeypatch.setattr(main, "PARENTAL_DECODO_BROWSER_ENABLED", True)
    monkeypatch.setattr(main, "PARENTAL_DECODO_BROWSER_HOST", "gate.decodo.com")
    monkeypatch.setattr(main, "PARENTAL_DECODO_BROWSER_PORT", 7000)
    monkeypatch.setattr(main, "PARENTAL_DECODO_BROWSER_USERNAME", "user-example")
    monkeypatch.setattr(main, "PARENTAL_DECODO_BROWSER_PASSWORD", "secret")
    monkeypatch.setattr(main, "PARENTAL_DECODO_BROWSER_COUNTRY", "us")
    monkeypatch.setattr(main, "PARENTAL_DECODO_BROWSER_SESSION_DURATION_MINUTES", 60)
    monkeypatch.setattr(main, "parental_decodo_browser_session_id", None)

    first_key, first_settings = main._playwright_proxy_identity(None)
    second_key, second_settings = main._playwright_proxy_identity(None)

    assert first_key == second_key
    assert first_settings == second_settings
    assert first_settings["server"] == "http://gate.decodo.com:7000"
    assert first_settings["password"] == "secret"
    assert first_settings["username"].startswith("user-example-country-us-session-")
    assert first_settings["username"].endswith("-sessionduration-60")


@pytest.mark.asyncio
async def test_fetch_parental_html_retries_with_second_proxy(monkeypatch):
    from fastapi import HTTPException

    import main

    sample_html = """
    <li class="ipc-metadata-list-item--link">
      <a>Sex &amp; Nudity:</a><div><div><div>Mild</div></div></div>
    </li>
    """
    proxies = iter(["http://proxy-a", "http://proxy-b"])

    monkeypatch.setattr(main, "PARENTAL_PROXY_ENABLED", True)
    monkeypatch.setattr(main, "PARENTAL_PROXY_RETRY_COUNT", 2)
    monkeypatch.setattr(main, "proxy_health", {})
    monkeypatch.setattr(main, "_choose_parental_proxy", lambda _exclude=None: next(proxies, None))

    async def fake_http(_imdb_id, proxy_url=None):
        if proxy_url == "http://proxy-a":
            raise HTTPException(status_code=502, detail="blocked")
        return "<html><body>accepted</body></html>"

    async def fake_browser(_imdb_id, proxy_url=None):
        if proxy_url == "http://proxy-a":
            raise HTTPException(status_code=502, detail="waf")
        return sample_html

    monkeypatch.setattr(main, "_fetch_parental_guide_html_via_http", fake_http)
    monkeypatch.setattr(main, "_fetch_parental_guide_html_via_browser", fake_browser)

    html = await main._fetch_parental_guide_html("tt0111161")
    assert "Sex &amp; Nudity" in html
    assert "http://proxy-a" in main.proxy_health


def test_get_chart_top_movies_returns_list(tmp_path, monkeypatch):
    import charts

    import main

    monkeypatch.setattr(main, "DB_PATH", tmp_path / "imdb.db")
    charts.chart_cache = {
        "top_movies": [
            {
                "tconst": "tt0111161",
                "primaryTitle": "Shawshank",
                "startYear": 1994,
                "averageRating": 9.3,
                "numVotes": 2800000,
                "rank": 1,
            },
        ]
    }
    client = TestClient(main.app)
    response = client.get("/chart/top_movies")
    assert response.status_code == 200
    data = response.json()
    assert data["chart"] == "top_movies"
    assert data["total"] == 1
    assert len(data["results"]) == 1
    assert data["results"][0]["tconst"] == "tt0111161"


def test_get_chart_respects_limit(tmp_path, monkeypatch):
    import charts

    import main

    monkeypatch.setattr(main, "DB_PATH", tmp_path / "imdb.db")
    charts.chart_cache = {
        "top_movies": [
            {
                "tconst": f"tt{i:07d}",
                "primaryTitle": f"Movie {i}",
                "startYear": 2000,
                "averageRating": 8.0,
                "numVotes": 50000,
                "rank": i,
            }
            for i in range(1, 11)
        ]
    }
    client = TestClient(main.app)
    response = client.get("/chart/top_movies?limit=3")
    assert response.status_code == 200
    assert len(response.json()["results"]) == 3


def test_get_chart_rejects_limit_above_max(tmp_path, monkeypatch):
    import charts

    import main

    monkeypatch.setattr(main, "DB_PATH", tmp_path / "imdb.db")
    charts.chart_cache = {"top_movies": []}
    client = TestClient(main.app)
    response = client.get("/chart/top_movies?limit=999")
    assert response.status_code == 400


def test_get_chart_returns_404_for_unknown_chart(tmp_path, monkeypatch):
    import charts

    import main

    monkeypatch.setattr(main, "DB_PATH", tmp_path / "imdb.db")
    charts.chart_cache = {}
    client = TestClient(main.app)
    response = client.get("/chart/nonexistent_chart")
    assert response.status_code == 404


def test_get_chart_rejects_limit_zero(tmp_path, monkeypatch):
    import charts

    import main

    monkeypatch.setattr(main, "DB_PATH", tmp_path / "imdb.db")
    charts.chart_cache = {"top_movies": []}
    client = TestClient(main.app)
    response = client.get("/chart/top_movies?limit=0")
    assert response.status_code == 400


def test_get_chart_returns_503_when_initializing(tmp_path, monkeypatch):
    import charts

    import main

    monkeypatch.setattr(main, "DB_PATH", tmp_path / "nonexistent.db")
    charts.chart_cache = {}
    client = TestClient(main.app, raise_server_exceptions=False)
    response = client.get("/chart/top_movies")
    assert response.status_code == 503


def _seed_search_db(db_path):
    """Seed DB with varied data for search tests."""
    conn = sqlite3.connect(db_path)
    from importer import create_schema

    create_schema(conn)
    titles = [
        ("tt0000001", "movie", "Action Film", "Action Film", 0, 2000, None, 120, "Action,Thriller"),
        ("tt0000002", "movie", "Comedy Film", "Comedy Film", 0, 2005, None, 90, "Comedy"),
        ("tt0000003", "tvSeries", "Drama Series", "Drama Series", 0, 2010, None, 45, "Drama"),
        ("tt0000004", "movie", "Short Film", "Short Film", 0, 2015, None, 10, "Short"),
        ("tt0000005", "movie", "Adult Film", "Adult Film", 1, 2018, None, 80, "Drama"),
    ]
    conn.executemany("INSERT INTO title_basics VALUES (?,?,?,?,?,?,?,?,?)", titles)
    ratings = [
        ("tt0000001", 8.5, 100000),
        ("tt0000002", 7.0, 50000),
        ("tt0000003", 9.0, 200000),
        ("tt0000004", 6.0, 5000),
        ("tt0000005", 5.0, 2000),
    ]
    conn.executemany("INSERT INTO title_ratings VALUES (?,?,?)", ratings)
    conn.commit()
    conn.close()


def test_search_filter_by_type(tmp_path, monkeypatch):
    db_path = tmp_path / "imdb.db"
    _seed_search_db(db_path)
    import main

    monkeypatch.setattr(main, "DB_PATH", db_path)
    client = TestClient(main.app)
    response = client.get("/search?type=movie")
    assert response.status_code == 200
    data = response.json()
    tconsts = set(data["results"])
    assert "tt0000001" in tconsts
    assert "tt0000003" not in tconsts  # tvSeries


def test_search_filter_by_genre_any(tmp_path, monkeypatch):
    db_path = tmp_path / "imdb.db"
    _seed_search_db(db_path)
    import main

    monkeypatch.setattr(main, "DB_PATH", db_path)
    client = TestClient(main.app)
    response = client.get("/search?genre.any=Action,Comedy")
    assert response.status_code == 200
    data = response.json()
    assert "tt0000001" in data["results"]
    assert "tt0000002" in data["results"]
    assert "tt0000003" not in data["results"]  # Drama only


def test_search_filter_by_rating_gte(tmp_path, monkeypatch):
    db_path = tmp_path / "imdb.db"
    _seed_search_db(db_path)
    import main

    monkeypatch.setattr(main, "DB_PATH", db_path)
    client = TestClient(main.app)
    response = client.get("/search?rating.gte=8")
    assert response.status_code == 200
    data = response.json()
    assert "tt0000001" in data["results"]  # 8.5
    assert "tt0000003" in data["results"]  # 9.0
    assert "tt0000002" not in data["results"]  # 7.0


def test_search_excludes_adult_by_default(tmp_path, monkeypatch):
    db_path = tmp_path / "imdb.db"
    _seed_search_db(db_path)
    import main

    monkeypatch.setattr(main, "DB_PATH", db_path)
    client = TestClient(main.app)
    response = client.get("/search")
    assert response.status_code == 200
    data = response.json()
    assert "tt0000005" not in data["results"]


def test_search_includes_adult_when_requested(tmp_path, monkeypatch):
    db_path = tmp_path / "imdb.db"
    _seed_search_db(db_path)
    import main

    monkeypatch.setattr(main, "DB_PATH", db_path)
    client = TestClient(main.app)
    response = client.get("/search?adult=true")
    assert response.status_code == 200
    data = response.json()
    assert "tt0000005" in data["results"]


def test_search_sort_by_year_asc(tmp_path, monkeypatch):
    db_path = tmp_path / "imdb.db"
    _seed_search_db(db_path)
    import main

    monkeypatch.setattr(main, "DB_PATH", db_path)
    client = TestClient(main.app)
    response = client.get("/search?sort_by=year.asc&limit=10")
    assert response.status_code == 200
    data = response.json()
    assert data["results"][0] == "tt0000001"  # 2000 is first


def test_search_limit(tmp_path, monkeypatch):
    db_path = tmp_path / "imdb.db"
    _seed_search_db(db_path)
    import main

    monkeypatch.setattr(main, "DB_PATH", db_path)
    client = TestClient(main.app)
    response = client.get("/search?limit=2")
    assert response.status_code == 200
    data = response.json()
    assert len(data["results"]) <= 2


def test_search_rejects_imdb_top_and_bottom_together(tmp_path, monkeypatch):
    import charts

    import main

    monkeypatch.setattr(main, "DB_PATH", tmp_path / "imdb.db")
    charts.chart_cache = {"top_movies": [], "lowest_rated": []}
    client = TestClient(main.app)
    response = client.get("/search?imdb_top=100&imdb_bottom=50")
    assert response.status_code == 400


def _seed_join_search_db(db_path):
    """Seed DB with data for join-filter search tests."""
    conn = sqlite3.connect(db_path)
    from importer import create_schema

    create_schema(conn)
    titles = [
        ("tt0000001", "movie", "English Film", "English Film", 0, 2000, None, 120, "Drama"),
        ("tt0000002", "movie", "French Film", "French Film", 0, 2001, None, 100, "Drama"),
        ("tt0000003", "movie", "Hindi Film", "Hindi Film", 0, 2002, None, 90, "Drama"),
        ("tt0000004", "tvEpisode", "Episode 1", "Episode 1", 0, 2010, None, 45, "Drama"),
    ]
    conn.executemany("INSERT INTO title_basics VALUES (?,?,?,?,?,?,?,?,?)", titles)
    conn.executemany(
        "INSERT INTO title_ratings VALUES (?,?,?)",
        [
            ("tt0000001", 8.0, 50000),
            ("tt0000002", 7.5, 40000),
            ("tt0000003", 7.0, 30000),
            ("tt0000004", 8.5, 10000),
        ],
    )
    conn.execute(
        "INSERT INTO title_akas VALUES ('tt0000001',1,'English Film','US','en',NULL,NULL,1)"
    )
    conn.execute(
        "INSERT INTO title_akas VALUES ('tt0000002',1,'French Film','FR','fr',NULL,NULL,1)"
    )
    conn.execute("INSERT INTO title_akas VALUES ('tt0000003',1,'Hindi Film','IN','hi',NULL,NULL,1)")
    conn.execute(
        "INSERT INTO title_principals VALUES ('tt0000001',1,'nm0000001','actor',NULL,'[\"Hero\"]')"
    )
    conn.execute(
        "INSERT INTO title_principals VALUES ('tt0000002',1,'nm0000002','actor',NULL,'[\"Villain\"]')"
    )
    conn.execute("INSERT INTO title_episode VALUES ('tt0000004','tt0000099',1,1)")
    conn.commit()
    conn.close()


def test_search_filter_by_language(tmp_path, monkeypatch):
    db_path = tmp_path / "imdb.db"
    _seed_join_search_db(db_path)
    import main

    monkeypatch.setattr(main, "DB_PATH", db_path)
    client = TestClient(main.app)
    response = client.get("/search?language=en")
    assert response.status_code == 200
    data = response.json()
    assert "tt0000001" in data["results"]
    assert "tt0000002" not in data["results"]


def test_search_filter_language_not(tmp_path, monkeypatch):
    db_path = tmp_path / "imdb.db"
    _seed_join_search_db(db_path)
    import main

    monkeypatch.setattr(main, "DB_PATH", db_path)
    client = TestClient(main.app)
    response = client.get("/search?language.not=fr&type=movie")
    assert response.status_code == 200
    data = response.json()
    assert "tt0000002" not in data["results"]
    assert "tt0000001" in data["results"]


def test_search_filter_by_cast(tmp_path, monkeypatch):
    db_path = tmp_path / "imdb.db"
    _seed_join_search_db(db_path)
    import main

    monkeypatch.setattr(main, "DB_PATH", db_path)
    client = TestClient(main.app)
    response = client.get("/search?cast=nm0000001")
    assert response.status_code == 200
    data = response.json()
    assert "tt0000001" in data["results"]
    assert "tt0000002" not in data["results"]


def test_search_filter_by_series(tmp_path, monkeypatch):
    db_path = tmp_path / "imdb.db"
    _seed_join_search_db(db_path)
    import main

    monkeypatch.setattr(main, "DB_PATH", db_path)
    client = TestClient(main.app)
    response = client.get("/search?series=tt0000099")
    assert response.status_code == 200
    data = response.json()
    assert "tt0000004" in data["results"]
    assert "tt0000001" not in data["results"]


def test_search_filter_type_not(tmp_path, monkeypatch):
    db_path = tmp_path / "imdb.db"
    _seed_search_db(db_path)
    import main

    monkeypatch.setattr(main, "DB_PATH", db_path)
    client = TestClient(main.app)
    response = client.get("/search?type.not=tvSeries")
    assert response.status_code == 200
    data = response.json()
    assert "tt0000003" not in data["results"]  # tvSeries excluded
    assert "tt0000001" in data["results"]


def test_search_filter_genre_all_must_match(tmp_path, monkeypatch):
    db_path = tmp_path / "imdb.db"
    _seed_search_db(db_path)
    import main

    monkeypatch.setattr(main, "DB_PATH", db_path)
    client = TestClient(main.app)
    # tt0000001 has Action,Thriller — genre=Action should match
    response = client.get("/search?genre=Action")
    assert response.status_code == 200
    data = response.json()
    assert "tt0000001" in data["results"]
    assert "tt0000002" not in data["results"]  # Comedy only


def test_search_filter_votes_gte(tmp_path, monkeypatch):
    db_path = tmp_path / "imdb.db"
    _seed_search_db(db_path)
    import main

    monkeypatch.setattr(main, "DB_PATH", db_path)
    client = TestClient(main.app)
    response = client.get("/search?votes.gte=100000")
    assert response.status_code == 200
    data = response.json()
    assert "tt0000001" in data["results"]  # 100000 votes
    assert "tt0000002" not in data["results"]  # 50000 votes


def test_search_filter_runtime(tmp_path, monkeypatch):
    db_path = tmp_path / "imdb.db"
    _seed_search_db(db_path)
    import main

    monkeypatch.setattr(main, "DB_PATH", db_path)
    client = TestClient(main.app)
    response = client.get("/search?runtime.gte=100")
    assert response.status_code == 200
    data = response.json()
    assert "tt0000001" in data["results"]  # 120 min
    assert "tt0000002" not in data["results"]  # 90 min


def test_search_filter_title(tmp_path, monkeypatch):
    db_path = tmp_path / "imdb.db"
    _seed_search_db(db_path)
    import main

    monkeypatch.setattr(main, "DB_PATH", db_path)
    client = TestClient(main.app)
    response = client.get("/search?title=Action")
    assert response.status_code == 200
    data = response.json()
    assert "tt0000001" in data["results"]
    assert "tt0000002" not in data["results"]


def test_search_invalid_sort_by_returns_400(tmp_path, monkeypatch):
    db_path = tmp_path / "imdb.db"
    _seed_search_db(db_path)
    import main

    monkeypatch.setattr(main, "DB_PATH", db_path)
    client = TestClient(main.app)
    response = client.get("/search?sort_by=popularity.desc")
    assert response.status_code == 400


def test_search_invalid_year_returns_400(tmp_path, monkeypatch):
    db_path = tmp_path / "imdb.db"
    _seed_search_db(db_path)
    import main

    monkeypatch.setattr(main, "DB_PATH", db_path)
    client = TestClient(main.app)
    response = client.get("/search?release.after=not-a-year")
    assert response.status_code == 400


def test_search_returns_503_when_no_db(tmp_path, monkeypatch):
    import main

    monkeypatch.setattr(main, "DB_PATH", tmp_path / "nonexistent.db")
    client = TestClient(main.app, raise_server_exceptions=False)
    response = client.get("/search")
    assert response.status_code == 503


def test_search_imdb_top_filters_to_chart(tmp_path, monkeypatch):
    import charts

    import main

    db_path = tmp_path / "imdb.db"
    _seed_search_db(db_path)
    monkeypatch.setattr(main, "DB_PATH", db_path)
    # Only tt0000001 is rank 1 in chart
    charts.chart_cache = {
        "top_movies": [
            {
                "tconst": "tt0000001",
                "rank": 1,
                "primaryTitle": "Action Film",
                "startYear": 2000,
                "averageRating": 8.5,
                "numVotes": 100000,
            },
        ]
    }
    client = TestClient(main.app)
    response = client.get("/search?imdb_top=1")
    assert response.status_code == 200
    data = response.json()
    assert "tt0000001" in data["results"]
    assert "tt0000002" not in data["results"]


@pytest.mark.asyncio
async def test_refresh_scheduler_calls_pipeline_at_correct_time():
    """Scheduler sleeps until REFRESH_HOUR, then calls _run_import_pipeline."""
    from datetime import datetime, timezone

    call_count = 0
    sleep_args = []

    # Mock "now" to be 10:00 UTC. REFRESH_HOUR defaults to 3.
    # So next target is tomorrow at 03:00 UTC.
    fake_now = datetime(2026, 3, 24, 10, 0, 0, tzinfo=timezone.utc)
    expected_target = datetime(2026, 3, 25, 3, 0, 0, tzinfo=timezone.utc)
    expected_sleep = (expected_target - fake_now).total_seconds()  # 17 hours

    async def fake_sleep(secs):
        sleep_args.append(secs)
        # Do not call asyncio.sleep here — it is patched and would recurse.
        # Returning immediately allows the scheduler to proceed to pipeline call.

    async def fake_pipeline():
        nonlocal call_count
        call_count += 1
        raise asyncio.CancelledError  # stop after first pipeline call

    import main

    with patch.object(main, "_run_import_pipeline", fake_pipeline):
        with patch.object(main.asyncio, "sleep", fake_sleep):
            with patch("main.datetime") as mock_dt:
                mock_dt.now.return_value = fake_now
                task = asyncio.create_task(main._refresh_scheduler())
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    assert call_count >= 1
    assert len(sleep_args) >= 1
    # Sleep should be approximately 17 hours (within 60 seconds of expected)
    assert abs(sleep_args[0] - expected_sleep) < 60


@pytest.mark.asyncio
async def test_refresh_scheduler_continues_after_pipeline_failure():
    """Scheduler logs the error and continues to next day rather than crashing."""
    call_count = 0

    async def failing_pipeline():
        nonlocal call_count
        call_count += 1
        raise RuntimeError("Download failed")

    async def fake_sleep(secs):
        if call_count >= 2:
            raise asyncio.CancelledError  # Stop after 2 iterations

    import main

    with patch.object(main, "_run_import_pipeline", failing_pipeline):
        with patch.object(main.asyncio, "sleep", fake_sleep):
            task = asyncio.create_task(main._refresh_scheduler())
            try:
                await task
            except asyncio.CancelledError:
                pass

    assert call_count >= 2  # Ran at least twice, proving it continues after failure


@pytest.mark.asyncio
async def test_initial_import_then_schedule_runs_pipeline_on_failure():
    """_initial_import_then_schedule logs failure and continues to scheduler."""
    pipeline_called = False
    scheduler_called = False

    async def failing_pipeline():
        nonlocal pipeline_called
        pipeline_called = True
        raise RuntimeError("Failed")

    async def fake_scheduler():
        nonlocal scheduler_called
        scheduler_called = True

    import main

    with patch.object(main, "_run_import_pipeline", failing_pipeline):
        with patch.object(main, "_refresh_scheduler", fake_scheduler):
            await main._initial_import_then_schedule()

    assert pipeline_called
    assert scheduler_called


def test_dashboard_renders_html(tmp_path, monkeypatch):
    import main

    monkeypatch.setattr(main, "ROOT_PATH", "")
    client = TestClient(main.app)
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    assert "IMDB Service Dashboard" in body
    assert "View API Documentation" in body
    # Embedded table metadata for all 7 tables
    assert "title_basics" in body
    assert "name_basics" in body
    assert "TABLE_META" in body


def test_dashboard_respects_root_path(monkeypatch):
    import main

    monkeypatch.setattr(main, "ROOT_PATH", "/imdb-service")
    client = TestClient(main.app)
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert "/imdb-service/" in response.text


def test_refresh_unknown_table_returns_404(monkeypatch):
    import main

    client = TestClient(main.app, raise_server_exceptions=False)
    response = client.post("/refresh/not_a_table")
    assert response.status_code == 404


def test_refresh_conflict_when_not_idle(monkeypatch):
    import main

    monkeypatch.setattr(main, "current_phase", "importing")
    client = TestClient(main.app, raise_server_exceptions=False)
    response = client.post("/refresh/title_basics")
    assert response.status_code == 409


def test_refresh_starts_background_task(monkeypatch):
    import main

    monkeypatch.setattr(main, "current_phase", "idle")

    called = {}

    async def fake_refresh(stem):
        called["stem"] = stem

    monkeypatch.setattr(main, "_refresh_single_table", fake_refresh)
    client = TestClient(main.app)
    response = client.post("/refresh/title_ratings")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "started"
    assert data["table"] == "title_ratings"


async def test_refresh_single_table_pipeline(tmp_path, monkeypatch):
    import main

    db_path = tmp_path / "imdb.db"

    downloaded = {}

    async def fake_download(data_dir, on_start=None, on_done=None, stems=None):
        downloaded["stems"] = stems
        return {"title.ratings": tmp_path / "title.ratings.tsv.gz"}, ["title.ratings"]

    imported = {}

    def fake_run_direct_import(gz_paths, live_db, changed_stems, *args):
        imported["changed_stems"] = changed_stems

    rebuilt = {}

    def fake_rebuild(db, votes):
        rebuilt["done"] = True

    import importer

    monkeypatch.setattr(importer, "download_datasets", fake_download)
    monkeypatch.setattr(importer, "run_direct_import", fake_run_direct_import)
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(main.charts, "rebuild_all_charts", fake_rebuild)
    monkeypatch.setattr(main, "current_phase", "idle")

    await main._refresh_single_table("title.ratings")

    assert downloaded["stems"] == ["title.ratings"]
    assert imported["changed_stems"] == ["title.ratings"]
    assert rebuilt["done"] is True
    assert main.current_phase == "idle"
