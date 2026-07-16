"""Pre-computed IMDB chart cache."""

import sqlite3
from pathlib import Path
from typing import Any, Callable, Optional, TypedDict

# Module-level chart cache. Replaced atomically by rebuild_all_charts().
chart_cache: dict[str, list[dict[str, Any]]] = {}


class ChartConfig(TypedDict):
    """Configuration for a single chart."""

    title_type: str
    aka_filter: tuple[str, str] | None
    ascending: bool


# Chart configs: name → {title_type, aka_filter (col, val) or None, ascending}
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


def rebuild_all_charts(
    db_path: Path,
    min_votes: int,
    on_progress: Optional[Callable[[str, int, int], None]] = None,
) -> None:
    """Recompute all charts and atomically replace chart_cache.

    If on_progress is provided, it is called before each chart computation with
    (chart_name, completed_count, total_count).
    """
    global chart_cache

    conn = sqlite3.connect(db_path)
    try:
        new_cache: dict[str, list[dict[str, Any]]] = {}
        total = len(CHART_CONFIGS)
        for index, (name, config) in enumerate(CHART_CONFIGS.items(), start=1):
            if on_progress:
                on_progress(name, index - 1, total)
            print(f"Computing chart: {name}...")
            new_cache[name] = _compute_chart(conn, config, min_votes)
            print(f"   {len(new_cache[name])} entries")
        chart_cache = new_cache
        print("Chart cache rebuilt")
    finally:
        conn.close()
