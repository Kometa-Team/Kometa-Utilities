"""Pre-computed IMDB chart cache."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, TypedDict, Union, cast

import httpx
from http_clients import GRAPHQL_HEADERS
from importer import SQLITE_BUSY_TIMEOUT_MS

# Module-level chart cache. Replaced atomically by rebuild_all_charts().
chart_cache: dict[str, list[dict[str, Any]]] = {}

# Per-chart last refresh timestamp (ISO 8601 UTC), also replaced atomically.
# For IMDb-sourced charts this is when IMDb served the IDs, not when the local
# cache object was rebuilt -- a chart held back on a stale fetch must report its
# true age rather than looking fresh.
chart_refreshed_at: dict[str, str] = {}

# Per-chart provenance: "imdb" (IMDb's own ranking) or "local" (Bayesian
# approximation used when IMDb is unreachable).  Exposed by the chart API so a
# degraded chart is visible to callers instead of silently passing as genuine.
chart_source: dict[str, str] = {}


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


# Local fallback configs: name -> {title_type, aka_filter (col, val) or None, ascending}.
# Every chart here also has an authoritative IMDb GraphQL source in
# GRAPHQL_CHART_CONFIGS; the Bayesian weighted rating below only approximates
# IMDb's published ranking and is used when the GraphQL fetch is unavailable.
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
    # chartTitles query: returns edges->node->id, has total field.
    # IMDb's published rating charts.  These are authoritative: the ranking is
    # IMDb's own, not a local approximation.
    "top_movies": {"query": "chartTitles", "chartType": "TOP_RATED_MOVIES", "first": 250},
    "top_shows": {"query": "chartTitles", "chartType": "TOP_RATED_TV_SHOWS", "first": 250},
    # IMDb's "Bottom 100" chart is 100 entries, not 250.
    "lowest_rated": {"query": "chartTitles", "chartType": "LOWEST_RATED_MOVIES", "first": 100},
    "top_english": {
        "query": "chartTitles",
        "chartType": "TOP_RATED_ENGLISH_MOVIES",
        "first": 250,
    },
    "top_indian": {"query": "chartTitles", "chartType": "TOP_RATED_INDIAN_MOVIES", "first": 250},
    "top_tamil": {"query": "chartTitles", "chartType": "TOP_RATED_TAMIL_MOVIES", "first": 250},
    "top_telugu": {"query": "chartTitles", "chartType": "TOP_RATED_TELUGU_MOVIES", "first": 250},
    "top_malayalam": {
        "query": "chartTitles",
        "chartType": "TOP_RATED_MALAYALAM_MOVIES",
        "first": 250,
    },
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

# Combined list of all chart names exposed by the service.  Charts with both a
# GraphQL source and a local fallback appear once, in CHART_CONFIGS order.
ALL_CHART_NAMES = list(dict.fromkeys(list(CHART_CONFIGS) + list(GRAPHQL_CHART_CONFIGS)))

# GraphQL chart IDs are fetched at most once per day.
GRAPHQL_CACHE_TTL_SECONDS = 86400

# Sidecar filename used to persist chart refresh timestamps.
CHART_CACHE_REFRESHED_FILE = "chart_cache_refreshed.json"

# Bump whenever a change to how charts are sourced or ranked makes previously
# persisted chart data wrong.  A cache written under a different version is
# discarded on load so the new ranking takes effect at startup instead of
# waiting for the next scheduled refresh.
#   2: rating charts switched from local Bayesian ranking to IMDb's GraphQL charts.
CHART_CACHE_VERSION = 2

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
        client = httpx.Client(timeout=30.0, headers=GRAPHQL_HEADERS)

    try:
        response = client.post(
            GRAPHQL_URL,
            headers=GRAPHQL_HEADERS,
            json={"query": query},
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            print(f"⚠️  GraphQL chart {name} returned errors: {payload['errors']}")
            return []
        return _extract_graphql_ids(name, payload)
    except Exception as e:  # noqa: BLE001
        print(f"⚠️  GraphQL chart fetch failed for {name}: {e}")
        return []
    finally:
        if close_client:
            client.close()


def _load_graphql_id_cache(
    cache_path: Path,
) -> tuple[Optional[datetime], dict[str, list[str]], dict[str, str]]:
    """Load the persisted GraphQL ID cache if it exists and is valid.

    Returns the whole-cache fetch time, the IDs, and the per-chart fetch times.
    A cache written before per-chart times existed falls back to the whole-cache
    time for every chart.
    """
    if not cache_path.exists():
        return None, {}, {}
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, {}, {}
        fetched_at = datetime.fromisoformat(data["fetched_at"])
        ids = data.get("ids", {})
        if not isinstance(ids, dict):
            return None, {}, {}
        per_chart = data.get("fetched_at_by_chart")
        if not isinstance(per_chart, dict):
            per_chart = {name: data["fetched_at"] for name in ids}
        return fetched_at, ids, per_chart
    except (json.JSONDecodeError, OSError, ValueError):
        return None, {}, {}


def _save_graphql_id_cache(
    cache_path: Path,
    fetched_at: datetime,
    ids: dict[str, list[str]],
    fetched_at_by_chart: dict[str, str],
) -> None:
    """Persist GraphQL chart IDs and their fetch timestamps.

    Per-chart timestamps are kept so that a chart retained from a stale cache
    keeps reporting when IMDb actually served it, rather than when the local
    chart cache was last rebuilt.
    """
    cache_path.write_text(
        json.dumps(
            {
                "fetched_at": fetched_at.isoformat(),
                "ids": ids,
                "fetched_at_by_chart": fetched_at_by_chart,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _fetch_all_graphql_chart_ids(client: httpx.Client) -> dict[str, list[str]]:
    """Fetch IMDb IDs for all GraphQL-backed charts in one shared session."""
    return {name: fetch_graphql_chart_ids(name, client=client) for name in GRAPHQL_CHART_CONFIGS}


def _get_graphql_chart_ids(
    graphql_cache_path: Optional[Path],
    client: httpx.Client,
    ttl_seconds: int = GRAPHQL_CACHE_TTL_SECONDS,
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Return GraphQL chart IDs and when IMDb actually served each of them.

    If the cache is stale or missing, fetch from IMDb and persist the result.
    If a chart's fetch returns no IDs but a stale cache exists, its stale IDs
    are kept so a temporary outage does not wipe the chart -- and its timestamp
    is kept with them, so a chart frozen on old data reports its true age.
    """
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    fetched_at: Optional[datetime] = None
    cached_ids: dict[str, list[str]] = {}
    cached_times: dict[str, str] = {}

    if graphql_cache_path is not None:
        fetched_at, cached_ids, cached_times = _load_graphql_id_cache(graphql_cache_path)

    # A cache written before a new chart was configured has no entry at all for
    # that chart, so treat it as stale rather than serving the chart empty for a
    # day.  Presence, not emptiness, is the test: a chart that legitimately
    # fetches empty must not force a refetch on every rebuild.
    covers_all_charts = all(name in cached_ids for name in GRAPHQL_CHART_CONFIGS)

    if (
        fetched_at is not None
        and covers_all_charts
        and (now - fetched_at).total_seconds() < ttl_seconds
    ):
        print("Using cached GraphQL chart IDs")
        return cached_ids, cached_times

    fetched_ids = _fetch_all_graphql_chart_ids(client)

    # Fall back per chart, not all-or-nothing: one chart failing should not
    # discard the fresh IDs for the others, and a chart that failed should keep
    # its last known good IDs rather than going empty.
    merged: dict[str, list[str]] = {}
    merged_times: dict[str, str] = {}
    for name in GRAPHQL_CHART_CONFIGS:
        fresh = fetched_ids.get(name) or []
        stale = cached_ids.get(name) or []
        if fresh:
            merged[name] = fresh
            merged_times[name] = now_iso
            continue
        if stale:
            stale_time = cached_times.get(name, now_iso)
            print(
                f"GraphQL fetch returned no IDs for {name}; "
                f"keeping stale cache from {stale_time}"
            )
            merged[name] = stale
            merged_times[name] = stale_time
        else:
            merged[name] = []

    if graphql_cache_path is not None:
        _save_graphql_id_cache(graphql_cache_path, now, merged, merged_times)

    return merged, merged_times


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


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open the dataset DB with a busy timeout so reads wait out a writer."""
    conn = sqlite3.connect(db_path)
    conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    return conn


def _chart_refreshed_path(cache_path: Path) -> Path:
    """Return the sidecar path for chart refresh timestamps."""
    return cache_path.with_name(CHART_CACHE_REFRESHED_FILE)


def save_chart_cache(cache_path: Path) -> None:
    """Persist the current chart cache to disk."""
    cache_path.write_text(json.dumps(chart_cache, indent=2), encoding="utf-8")
    _chart_refreshed_path(cache_path).write_text(
        json.dumps(
            {
                "version": CHART_CACHE_VERSION,
                "refreshed_at": chart_refreshed_at,
                "source": chart_source,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def load_chart_cache(cache_path: Path) -> bool:
    """Load chart cache from disk if it exists and is valid.

    Returns True if the cache was loaded successfully.
    """
    global chart_cache, chart_refreshed_at, chart_source

    if not cache_path.exists():
        return False
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return False
        # Validate that all expected chart keys are present.
        if set(data.keys()) != set(ALL_CHART_NAMES):
            return False
        # The sidecar carries the version this cache was written under.  A
        # missing or older sidecar means the data predates the current chart
        # sourcing, so discard it and let the caller rebuild.
        refreshed_path = _chart_refreshed_path(cache_path)
        if not refreshed_path.exists():
            return False
        sidecar = json.loads(refreshed_path.read_text(encoding="utf-8"))
        if not isinstance(sidecar, dict) or sidecar.get("version") != CHART_CACHE_VERSION:
            print("Chart cache was written by a different version; rebuilding")
            return False

        chart_cache = data
        chart_refreshed_at = sidecar.get("refreshed_at", {})
        chart_source = sidecar.get("source", {})
        return True
    except (json.JSONDecodeError, OSError):
        return False


def _cache_is_fresh(cache_path: Path, db_path: Path) -> bool:
    """Return True if the cache file exists and its modification time is newer than the DB's last_refresh."""
    if not cache_path.exists():
        return False

    try:
        conn = _connect(db_path)
        cursor = conn.execute("SELECT value FROM import_meta WHERE key = 'last_refresh'")
        row = cursor.fetchone()
        conn.close()

        if not row:
            return False

        last_refresh_dt = datetime.fromisoformat(row[0])
        if last_refresh_dt.tzinfo is None:
            last_refresh_dt = last_refresh_dt.replace(tzinfo=timezone.utc)

        cache_mtime = cache_path.stat().st_mtime
        cache_dt = datetime.fromtimestamp(cache_mtime, tz=timezone.utc)

        return cache_dt >= last_refresh_dt
    except Exception:
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
    global chart_cache, chart_refreshed_at, chart_source

    conn = _connect(db_path)
    client: Optional[httpx.Client] = None
    graphql_ids: dict[str, list[str]] = {}
    graphql_times: dict[str, str] = {}
    if fetch_graphql:
        client = httpx.Client(timeout=30.0, headers=GRAPHQL_HEADERS)
        graphql_ids, graphql_times = _get_graphql_chart_ids(graphql_cache_path, client)

    try:
        new_cache: dict[str, list[dict[str, Any]]] = {}
        new_refreshed: dict[str, str] = {}
        new_source: dict[str, str] = {}
        total = len(ALL_CHART_NAMES)
        now = datetime.now(timezone.utc).isoformat()

        for index, name in enumerate(ALL_CHART_NAMES, start=1):
            if on_progress:
                on_progress(name, index - 1, total)
            print(f"Computing chart: {name}...")

            # IMDb's own ranking wins when we have it; the local Bayesian
            # computation is only an approximation of these charts.
            ids = graphql_ids.get(name, []) if fetch_graphql else []
            if ids:
                new_cache[name] = _enrich_chart_ids(conn, ids)
                source = "imdb"
                # Age the chart from IMDb's fetch, not this rebuild.
                refreshed = graphql_times.get(name, now)
            elif name in CHART_CONFIGS:
                new_cache[name] = _compute_chart(conn, CHART_CONFIGS[name], min_votes)
                source = "local"
                refreshed = now
            else:
                new_cache[name] = []
                source = "unavailable"
                refreshed = now

            new_refreshed[name] = refreshed
            new_source[name] = source
            print(f"   {len(new_cache[name])} entries ({source}, as of {refreshed})")

        chart_cache = new_cache
        chart_refreshed_at = new_refreshed
        chart_source = new_source
        if cache_path:
            save_chart_cache(cache_path)
        print("Chart cache rebuilt")
    finally:
        conn.close()
        if client is not None:
            client.close()
