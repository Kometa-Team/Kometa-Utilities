"""Pre-computed IMDB chart cache."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, TypedDict, Union, cast

import httpx

# Module-level chart cache. Replaced atomically by rebuild_all_charts().
chart_cache: dict[str, list[dict[str, Any]]] = {}

# Per-chart last refresh timestamp (ISO 8601 UTC), also replaced atomically.
chart_refreshed_at: dict[str, str] = {}


class ChartConfig(TypedDict):
    """Configuration for a single locally-computed chart."""

    title_type: str
    aka_filter: tuple[str, str] | None
    ascending: bool


class GraphQLChartConfig(TypedDict):
    """Configuration for a single IMDb GraphQL chart."""

    query: str
    chartType: str
    predefined: str
    first: int


# Chart configs: name -> {title_type, aka_filter (col, val) or None, ascending}
CHART_CONFIGS: dict[str, ChartConfig] = {
    "top_movies": {"title_type": "movie", "aka_filter": None, "ascending": False},
    "top_shows": {"title_type": "tvSeries", "aka_filter": None, "ascending": False},
    "lowest_rated": {"title_type": "movie", "aka_filter": None, "ascending": True},
    "top_english": {"title_type": "movie", "aka_filter": ("language", "en"), "ascending": False},
    "top_indian": {"title_type": "movie", "aka_filter": ("region", "IN"), "ascending": False},
    "top_tamil": {"title_type": "movie", "aka_filter": ("language", "ta"), "ascending": False},
    "top_telugu": {"title_type": "movie", "aka_filter": ("language", "te"), "ascending": False},
    "top_malayalam": {
        "title_type": "movie",
        "aka_filter": ("language", "ml"),
        "ascending": False,
    },
}

# GraphQL chart configs ported from Kometa's modules/imdb.py.
GRAPHQL_URL = "https://api.graphql.imdb.com/"
GRAPHQL_CHART_CONFIGS: dict[str, dict[str, Union[str, int]]] = {
    # chartTitles query: returns edges->node->id, has total field
    "popular_movies": {"query": "chartTitles", "chartType": "MOST_POPULAR_MOVIES", "first": 100},
    "popular_shows": {"query": "chartTitles", "chartType": "MOST_POPULAR_TV_SHOWS", "first": 100},
    # boxOfficeWeekendChart query: returns entries->title->id
    "box_office": {"query": "boxOfficeWeekendChart"},
    # topTrendingSetsPredefined query: returns edges->node->item->...on Title->id
    "trending_india": {
        "query": "topTrendingSetsPredefined",
        "predefined": "INDIA_TITLE_TRENDS_UPCOMING",
        "first": 50,
    },
    "trending_tamil": {
        "query": "topTrendingSetsPredefined",
        "predefined": "INDIA_TITLE_TRENDS_RELEASED_TAMIL",
        "first": 200,
    },
    "trending_telugu": {
        "query": "topTrendingSetsPredefined",
        "predefined": "INDIA_TITLE_TRENDS_RELEASED_TELUGU",
        "first": 200,
    },
}

# Combined list of all chart names exposed by the service.
ALL_CHART_NAMES = list(CHART_CONFIGS.keys()) + list(GRAPHQL_CHART_CONFIGS.keys())

# GraphQL chart IDs are fetched at most once per day.
GRAPHQL_CACHE_TTL_SECONDS = 86400

# Sidecar filename used to persist chart refresh timestamps.
CHART_CACHE_REFRESHED_FILE = "chart_cache_refreshed.json"

DEFAULT_CHART_SIZE = 250
MAX_CHART_SIZE = 500


def _compute_chart(
    conn: sqlite3.Connection,
    config: ChartConfig,
    min_votes: int,
    limit: int = DEFAULT_CHART_SIZE,
) -> list[dict[str, Any]]:
    """Compute a single chart using the Bayesian weighted rating formula."""
    title_type = config["title_type"]
    aka_filter = config["aka_filter"]
    ascending = config["ascending"]

    if aka_filter:
        aka_col, aka_val = aka_filter
        sql = f"""
            SELECT tb.tconst, tb.primaryTitle, tb.startYear, tr.averageRating, tr.numVotes
            FROM title_basics tb
            JOIN title_ratings tr ON tb.tconst = tr.tconst
            WHERE tb.titleType = ?
              AND tr.numVotes >= ?
              AND EXISTS (
                  SELECT 1 FROM title_akas ta
                  WHERE ta.tconst = tb.tconst AND ta.{aka_col} = ?
              )
        """  # nosec B608 — aka_col is from internal CHART_CONFIGS dict, not user input
        params: tuple[str, int, str] | tuple[str, int] = (title_type, min_votes, aka_val)
    else:
        sql = """
            SELECT tb.tconst, tb.primaryTitle, tb.startYear, tr.averageRating, tr.numVotes
            FROM title_basics tb
            JOIN title_ratings tr ON tb.tconst = tr.tconst
            WHERE tb.titleType = ?
              AND tr.numVotes >= ?
        """
        params = (title_type, min_votes)

    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return []

    # C = mean rating across all qualifying titles
    mean_rating = sum(r[3] for r in rows) / len(rows)
    m = min_votes

    def wr(r: float, v: int) -> float:
        return float((v / (v + m)) * r + (m / (v + m)) * mean_rating)

    scored = sorted(
        rows,
        key=lambda row: wr(row[3], row[4]),
        reverse=not ascending,
    )

    return [
        {
            "tconst": row[0],
            "primaryTitle": row[1],
            "startYear": row[2],
            "averageRating": row[3],
            "numVotes": row[4],
            "rank": rank,
        }
        for rank, row in enumerate(scored[:limit], start=1)
    ]


def _build_graphql_query(name: str) -> str:
    """Build the GraphQL query string for a Kometa-style chart."""
    cfg = GRAPHQL_CHART_CONFIGS[name]
    query_type = cfg["query"]

    if query_type == "chartTitles":
        chart_type = cast(str, cfg["chartType"])
        first = cast(int, cfg["first"])
        return f"{{ chartTitles(chart: {{ chartType: {chart_type} }}, first: {first}) {{ edges {{ node {{ id }} }} total }} }}"
    if query_type == "boxOfficeWeekendChart":
        return "{ boxOfficeWeekendChart(limit: 50) { entries { title { id } } } }"
    if query_type == "topTrendingSetsPredefined":
        first = cast(int, cfg["first"])
        predefined = cast(str, cfg["predefined"])
        return (
            f"{{ topTrendingSetsPredefined(first: {first}, input: {{ topTrendingSetPredefined: {predefined} }}) "
            f"{{ edges {{ node {{ item {{ ... on Title {{ id }} }} }} }} }} }}"
        )
    raise ValueError(f"Unknown GraphQL chart query type: {query_type}")


def _extract_graphql_ids(name: str, payload: dict[str, Any]) -> list[str]:
    """Extract IMDb IDs from a GraphQL chart response."""
    cfg = GRAPHQL_CHART_CONFIGS[name]
    query_type = cfg["query"]
    data = payload.get("data", {})

    if query_type == "chartTitles":
        return [edge["node"]["id"] for edge in data.get("chartTitles", {}).get("edges", [])]
    if query_type == "boxOfficeWeekendChart":
        return [
            entry["title"]["id"]
            for entry in data.get("boxOfficeWeekendChart", {}).get("entries", [])
        ]
    if query_type == "topTrendingSetsPredefined":
        return [
            edge["node"]["item"]["id"]
            for edge in data.get("topTrendingSetsPredefined", {}).get("edges", [])
            if edge.get("node", {}).get("item")
        ]
    return []


def fetch_graphql_chart_ids(name: str, client: Optional[httpx.Client] = None) -> list[str]:
    """Fetch IMDb IDs for a GraphQL-backed chart.

    If a client is not provided, a short-lived one is created.  Network or API
    errors are logged and return an empty list so that chart rebuilds stay
    resilient.
    """
    if name not in GRAPHQL_CHART_CONFIGS:
        return []

    query = _build_graphql_query(name)
    close_client = client is None
    if client is None:
        client = httpx.Client(timeout=30.0)

    try:
        response = client.post(
            GRAPHQL_URL,
            headers={"content-type": "application/json"},
            json={"query": query},
        )
        response.raise_for_status()
        return _extract_graphql_ids(name, response.json())
    except Exception as e:  # noqa: BLE001
        print(f"⚠️  GraphQL chart fetch failed for {name}: {e}")
        return []
    finally:
        if close_client:
            client.close()


def _load_graphql_id_cache(
    cache_path: Path,
) -> tuple[Optional[datetime], dict[str, list[str]]]:
    """Load the persisted GraphQL ID cache if it exists and is valid."""
    if not cache_path.exists():
        return None, {}
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, {}
        fetched_at = datetime.fromisoformat(data["fetched_at"])
        ids = data.get("ids", {})
        if not isinstance(ids, dict):
            return None, {}
        return fetched_at, ids
    except (json.JSONDecodeError, OSError, ValueError):
        return None, {}


def _save_graphql_id_cache(
    cache_path: Path,
    fetched_at: datetime,
    ids: dict[str, list[str]],
) -> None:
    """Persist GraphQL chart IDs and their fetch timestamp."""
    cache_path.write_text(
        json.dumps({"fetched_at": fetched_at.isoformat(), "ids": ids}, indent=2),
        encoding="utf-8",
    )


def _fetch_all_graphql_chart_ids(client: httpx.Client) -> dict[str, list[str]]:
    """Fetch IMDb IDs for all GraphQL-backed charts in one shared session."""
    return {name: fetch_graphql_chart_ids(name, client=client) for name in GRAPHQL_CHART_CONFIGS}


def _get_graphql_chart_ids(
    graphql_cache_path: Optional[Path],
    client: httpx.Client,
    ttl_seconds: int = GRAPHQL_CACHE_TTL_SECONDS,
) -> dict[str, list[str]]:
    """Return GraphQL chart IDs, using a cached copy when it is still fresh.

    If the cache is stale or missing, fetch from IMDb and persist the result.
    If the fetch returns no IDs at all but a stale cache exists, the stale IDs
    are kept so a temporary outage does not wipe the charts.
    """
    now = datetime.now(timezone.utc)
    fetched_at: Optional[datetime] = None
    cached_ids: dict[str, list[str]] = {}

    if graphql_cache_path is not None:
        fetched_at, cached_ids = _load_graphql_id_cache(graphql_cache_path)

    if fetched_at is not None and (now - fetched_at).total_seconds() < ttl_seconds:
        print("Using cached GraphQL chart IDs")
        return cached_ids

    fetched_ids = _fetch_all_graphql_chart_ids(client)

    if graphql_cache_path is not None and not any(fetched_ids.values()) and cached_ids:
        print("GraphQL fetch returned no IDs; keeping stale cache")
        return cached_ids

    if graphql_cache_path is not None:
        _save_graphql_id_cache(graphql_cache_path, now, fetched_ids)

    return fetched_ids


def _enrich_chart_ids(
    conn: Optional[sqlite3.Connection],
    ids: list[str],
) -> list[dict[str, Any]]:
    """Turn a list of IMDb IDs into chart items, enriching from the DB when possible."""
    items: list[dict[str, Any]] = []
    for rank, imdb_id in enumerate(ids, start=1):
        item: dict[str, Any] = {"tconst": imdb_id, "rank": rank}
        if conn is not None:
            basics = conn.execute(
                "SELECT primaryTitle, startYear FROM title_basics WHERE tconst = ?", (imdb_id,)
            ).fetchone()
            if basics:
                item["primaryTitle"] = basics[0]
                item["startYear"] = basics[1]
            ratings = conn.execute(
                "SELECT averageRating, numVotes FROM title_ratings WHERE tconst = ?", (imdb_id,)
            ).fetchone()
            if ratings:
                item["averageRating"] = ratings[0]
                item["numVotes"] = ratings[1]
        items.append(item)
    return items


def _compute_graphql_chart(
    conn: Optional[sqlite3.Connection],
    name: str,
    client: Optional[httpx.Client] = None,
) -> list[dict[str, Any]]:
    """Fetch and optionally enrich a GraphQL-backed chart."""
    ids = fetch_graphql_chart_ids(name, client=client)
    return _enrich_chart_ids(conn, ids)


def _chart_refreshed_path(cache_path: Path) -> Path:
    """Return the sidecar path for chart refresh timestamps."""
    return cache_path.with_name(CHART_CACHE_REFRESHED_FILE)


def save_chart_cache(cache_path: Path) -> None:
    """Persist the current chart cache to disk."""
    cache_path.write_text(json.dumps(chart_cache, indent=2), encoding="utf-8")
    _chart_refreshed_path(cache_path).write_text(
        json.dumps(chart_refreshed_at, indent=2), encoding="utf-8"
    )


def load_chart_cache(cache_path: Path) -> bool:
    """Load chart cache from disk if it exists and is valid.

    Returns True if the cache was loaded successfully.
    """
    global chart_cache, chart_refreshed_at

    if not cache_path.exists():
        return False
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return False
        # Validate that all expected chart keys are present.
        if set(data.keys()) != set(ALL_CHART_NAMES):
            return False
        chart_cache = data

        refreshed_path = _chart_refreshed_path(cache_path)
        if refreshed_path.exists():
            chart_refreshed_at = json.loads(refreshed_path.read_text(encoding="utf-8"))
        else:
            mtime = cache_path.stat().st_mtime
            fallback = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
            chart_refreshed_at = {name: fallback for name in ALL_CHART_NAMES}
        return True
    except (json.JSONDecodeError, OSError):
        return False


def _cache_is_fresh(cache_path: Path, db_path: Path) -> bool:
    """Return True if the cache file exists and is newer than the DB file."""
    if not cache_path.exists():
        return False
    try:
        return cache_path.stat().st_mtime >= db_path.stat().st_mtime
    except OSError:
        return False


def rebuild_all_charts(
    db_path: Path,
    min_votes: int,
    on_progress: Optional[Callable[[str, int, int], None]] = None,
    cache_path: Optional[Path] = None,
    fetch_graphql: bool = False,
    graphql_cache_path: Optional[Path] = None,
) -> None:
    """Recompute all charts and atomically replace chart_cache.

    If on_progress is provided, it is called before each chart computation with
    (chart_name, completed_count, total_count).

    If cache_path is provided, the computed cache is persisted to that file.

    GraphQL-backed charts (popular_movies, box_office, trending_*, etc.) are
    only fetched from IMDb when fetch_graphql=True to keep tests deterministic
    and offline by default.  When fetch_graphql=True, graphql_cache_path may be
    provided to cache the raw IMDb IDs for 24 hours.
    """
    global chart_cache, chart_refreshed_at

    conn = sqlite3.connect(db_path)
    client: Optional[httpx.Client] = None
    graphql_ids: dict[str, list[str]] = {}
    if fetch_graphql:
        client = httpx.Client(timeout=30.0)
        graphql_ids = _get_graphql_chart_ids(graphql_cache_path, client)

    try:
        new_cache: dict[str, list[dict[str, Any]]] = {}
        new_refreshed: dict[str, str] = {}
        total = len(ALL_CHART_NAMES)
        now = datetime.now(timezone.utc).isoformat()

        # Locally-computed charts.
        for index, (name, config) in enumerate(CHART_CONFIGS.items(), start=1):
            if on_progress:
                on_progress(name, index - 1, total)
            print(f"Computing chart: {name}...")
            new_cache[name] = _compute_chart(conn, config, min_votes)
            new_refreshed[name] = now
            print(f"   {len(new_cache[name])} entries")

        # GraphQL-backed charts.
        for index, name in enumerate(GRAPHQL_CHART_CONFIGS.keys(), start=len(CHART_CONFIGS) + 1):
            if on_progress:
                on_progress(name, index - 1, total)
            print(f"Computing chart: {name}...")
            if fetch_graphql:
                new_cache[name] = _enrich_chart_ids(conn, graphql_ids.get(name, []))
            else:
                new_cache[name] = []
            new_refreshed[name] = now
            print(f"   {len(new_cache[name])} entries")

        chart_cache = new_cache
        chart_refreshed_at = new_refreshed
        if cache_path:
            save_chart_cache(cache_path)
        print("Chart cache rebuilt")
    finally:
        conn.close()
        if client is not None:
            client.close()
