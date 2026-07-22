"""SIMKL Service - FastAPI caching proxy for SIMKL metadata lists."""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite
import httpx
from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
LISTS_DIR = DATA_DIR / "lists"
ROOT_PATH = os.getenv("ROOT_PATH", "")
LOGO_PATH = Path(__file__).resolve().parent / "Simkl_logo.svg"

BASE_URL = "https://data.simkl.in"

# Each list config: key, list-file stem, upstream path, kind (trending|dvd)
LIST_CONFIGS: List[Dict[str, str]] = [
    {
        "key": "trending_today_small",
        "stem": "trending_today_100",
        "path": "/discover/trending/today_100.json",
        "kind": "trending",
    },
    {
        "key": "trending_today_large",
        "stem": "trending_today_500",
        "path": "/discover/trending/today_500.json",
        "kind": "trending",
    },
    {
        "key": "trending_week_small",
        "stem": "trending_week_100",
        "path": "/discover/trending/week_100.json",
        "kind": "trending",
    },
    {
        "key": "trending_week_large",
        "stem": "trending_week_500",
        "path": "/discover/trending/week_500.json",
        "kind": "trending",
    },
    {
        "key": "trending_month_small",
        "stem": "trending_month_100",
        "path": "/discover/trending/month_100.json",
        "kind": "trending",
    },
    {
        "key": "trending_month_large",
        "stem": "trending_month_500",
        "path": "/discover/trending/month_500.json",
        "kind": "trending",
    },
    {
        "key": "dvd_small",
        "stem": "dvd_100",
        "path": "/discover/dvd/releases_100.json",
        "kind": "dvd",
    },
    {
        "key": "dvd_large",
        "stem": "dvd_500",
        "path": "/discover/dvd/releases_500.json",
        "kind": "dvd",
    },
]

KEY_TO_CONFIG: Dict[str, Dict[str, str]] = {c["key"]: c for c in LIST_CONFIGS}

# Type subdirectory names used on disk
TYPE_DIRS = ("movies", "tv", "anime")

# Mapping of JSON array key → type name (used in trending lists)
TRENDING_TYPE_MAP = {"movies": "movies", "tv": "tv", "anime": "anime"}

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
refresh_queue: Optional[asyncio.Queue] = None
pending_keys: set = set()
worker_task: Optional[asyncio.Task] = None


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
async def init_database() -> None:
    """Initialise the SQLite database with the items table and indexes."""
    db_path = DATA_DIR / "simkl.db"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS items (
                simkl_id   INTEGER PRIMARY KEY,
                type       TEXT NOT NULL,
                title      TEXT,
                imdb_id    TEXT,
                tmdb_id    TEXT,
                tvdb_id    TEXT,
                mal_id     TEXT,
                anilist_id TEXT,
                anidb_id   TEXT,
                kitsu_id   TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_items_imdb    ON items(imdb_id);
            CREATE INDEX IF NOT EXISTS idx_items_tmdb    ON items(tmdb_id);
            CREATE INDEX IF NOT EXISTS idx_items_tvdb    ON items(tvdb_id);
            CREATE INDEX IF NOT EXISTS idx_items_mal     ON items(mal_id);
            CREATE INDEX IF NOT EXISTS idx_items_anilist ON items(anilist_id);
            CREATE INDEX IF NOT EXISTS idx_items_anidb   ON items(anidb_id);
            CREATE INDEX IF NOT EXISTS idx_items_kitsu   ON items(kitsu_id);
            CREATE INDEX IF NOT EXISTS idx_items_type    ON items(type);
            """
        )
        await db.commit()


async def upsert_items(items: List[Dict[str, Any]]) -> None:
    """Upsert a batch of items into the database."""
    db_path = DATA_DIR / "simkl.db"
    rows = []
    for item in items:
        ids = item.get("ids", {})
        rows.append(
            (
                ids.get("simkl_id"),
                item.get("_type"),
                item.get("title"),
                ids.get("imdb"),
                str(ids.get("tmdb")) if ids.get("tmdb") else None,
                str(ids.get("tvdb")) if ids.get("tvdb") else None,
                str(ids.get("mal")) if ids.get("mal") else None,
                str(ids.get("anilist")) if ids.get("anilist") else None,
                str(ids.get("anidb")) if ids.get("anidb") else None,
                str(ids.get("kitsu")) if ids.get("kitsu") else None,
            )
        )
    async with aiosqlite.connect(db_path) as db:
        await db.executemany(
            """
            INSERT OR REPLACE INTO items
                (simkl_id, type, title, imdb_id, tmdb_id, tvdb_id,
                 mal_id, anilist_id, anidb_id, kitsu_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [r for r in rows if r[0] is not None],
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------
def is_fresh(path: Path) -> bool:
    """Return True if the file exists and was last modified today (UTC)."""
    if not path.exists():
        return False
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).date()
    return mtime >= datetime.now(tz=timezone.utc).date()


def extract_items_from_list(raw: Any, kind: str) -> List[Dict[str, Any]]:
    """Extract individual items from a raw list payload.

    Trending lists have top-level keys ``movies``, ``tv``, ``anime``.
    DVD lists are a flat array of movie items.
    Returns items with a ``_type`` field injected for downstream use.
    """
    items: List[Dict[str, Any]] = []
    if kind == "trending":
        for json_key, item_type in TRENDING_TYPE_MAP.items():
            for item in raw.get(json_key) or []:
                item = dict(item)
                item["_type"] = item_type
                items.append(item)
    else:  # dvd
        for item in raw if isinstance(raw, list) else []:
            item = dict(item)
            item["_type"] = "movies"
            items.append(item)
    return items


async def save_items_to_disk(items: List[Dict[str, Any]]) -> None:
    """Write each item to its type subdirectory as ``{simkl_id}.json``."""
    for item in items:
        item_type = item.get("_type", "movies")
        ids = item.get("ids", {})
        simkl_id = ids.get("simkl_id")
        if simkl_id is None:
            continue
        dest_dir = DATA_DIR / item_type
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / f"{simkl_id}.json").write_text(
            json.dumps(item, ensure_ascii=False), encoding="utf-8"
        )


# ---------------------------------------------------------------------------
# Fetch & cache
# ---------------------------------------------------------------------------
async def fetch_and_cache_list(key: str) -> None:
    """Fetch a list from SIMKL, persist it, and extract individual items."""
    cfg = KEY_TO_CONFIG.get(key)
    if cfg is None:
        return

    url = BASE_URL + cfg["path"]
    list_file = LISTS_DIR / f"{cfg['stem']}.json"

    try:
        async with httpx.AsyncClient(
            timeout=30.0, headers={"User-Agent": "kometa/1.0"}, follow_redirects=True
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            raw = response.json()

        LISTS_DIR.mkdir(parents=True, exist_ok=True)
        list_file.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        items = extract_items_from_list(raw, cfg["kind"])
        await save_items_to_disk(items)
        await upsert_items(items)

        print(f"✅ Cached {key} ({len(items)} items)")
    except Exception as e:
        print(f"❌ Failed to fetch {key}: {e}")


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------
async def refresh_worker() -> None:
    """Process stale-list refresh requests from the background queue."""
    print("🚀 SIMKL refresh worker started")
    while True:
        key = ""
        try:
            key = await refresh_queue.get()
            if key in pending_keys:
                pending_keys.discard(key)
            print(f"⏳ Refreshing {key}…")
            await fetch_and_cache_list(key)
            refresh_queue.task_done()
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"❌ Worker error for {key}: {e}")
            refresh_queue.task_done()


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[type-arg]
    """Manage application lifespan: initialise dirs/DB and seed the refresh queue."""
    global worker_task, refresh_queue

    print("🔧 Initialising SIMKL Service…")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LISTS_DIR.mkdir(parents=True, exist_ok=True)
    for type_dir in TYPE_DIRS:
        (DATA_DIR / type_dir).mkdir(parents=True, exist_ok=True)

    await init_database()

    refresh_queue = asyncio.Queue()
    worker_task = asyncio.create_task(refresh_worker())

    # Queue any stale or missing lists
    for cfg in LIST_CONFIGS:
        list_file = LISTS_DIR / f"{cfg['stem']}.json"
        if not is_fresh(list_file):
            key = cfg["key"]
            pending_keys.add(key)
            refresh_queue.put_nowait(key)

    print("✅ SIMKL Service ready")
    yield

    # Shutdown
    print("🛑 Shutting down SIMKL Service…")
    if worker_task:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="SIMKL Service",
    description="Caching proxy for SIMKL trending and DVD release lists.",
    lifespan=lifespan,
    root_path=ROOT_PATH,
)


# ---------------------------------------------------------------------------
# Helpers used by route handlers
# ---------------------------------------------------------------------------
async def serve_list(key: str) -> JSONResponse:
    """Return a cached list, refreshing in the background when stale."""
    cfg = KEY_TO_CONFIG[key]
    list_file = LISTS_DIR / f"{cfg['stem']}.json"

    if is_fresh(list_file):
        data = json.loads(list_file.read_text(encoding="utf-8"))
        return JSONResponse(content=data, headers={"X-Cache": "HIT"})

    if list_file.exists():
        # Serve stale copy; queue background refresh
        if key not in pending_keys:
            pending_keys.add(key)
            await refresh_queue.put(key)
        data = json.loads(list_file.read_text(encoding="utf-8"))
        return JSONResponse(content=data, headers={"X-Cache": "STALE"})

    # Missing — fetch synchronously so the caller gets a response
    await fetch_and_cache_list(key)
    if not list_file.exists():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to fetch list from SIMKL",
        )
    data = json.loads(list_file.read_text(encoding="utf-8"))
    return JSONResponse(content=data, headers={"X-Cache": "MISS"})


async def serve_item(item_type: str, simkl_id: int) -> JSONResponse:
    """Return a single cached item JSON file or 404."""
    item_file = DATA_DIR / item_type / f"{simkl_id}.json"
    if not item_file.exists():
        raise HTTPException(status_code=404, detail=f"{item_type} item {simkl_id} not found")
    return JSONResponse(content=json.loads(item_file.read_text(encoding="utf-8")))


async def find_item(item_type: str, filters: Dict[str, Optional[str]]) -> JSONResponse:
    """Look up a single item by external ID filters (AND logic)."""
    active = {col: val for col, val in filters.items() if val is not None}
    if not active:
        raise HTTPException(status_code=422, detail="At least one query parameter is required")

    db_path = DATA_DIR / "simkl.db"
    # Column names come from the hardcoded `filters` dict (never user input);
    # all values are passed via parameterised placeholders — no injection risk.
    clauses = " AND ".join(f"{col} = ?" for col in active)
    sql = f"SELECT simkl_id FROM items WHERE type = ? AND {clauses}"  # noqa: S608  # nosec B608
    params = [item_type] + list(active.values())

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(sql, params)
        row = await cursor.fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="Item not found")

    return await serve_item(item_type, row[0])


# ---------------------------------------------------------------------------
# Root / info
# ---------------------------------------------------------------------------
@app.get("/logo.svg", include_in_schema=False)
async def logo() -> FileResponse:
    """Return the Simkl service logo."""
    return FileResponse(
        LOGO_PATH,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/", response_class=HTMLResponse)
async def root(request: Request) -> HTMLResponse:
    """HTML info page."""
    base = f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"
    if ROOT_PATH:
        base += ROOT_PATH
    html = f"""<!DOCTYPE html>
<html><head><title>SIMKL Service</title>
<link rel="icon" href="{base}/logo.svg?v=5d5f50ea" type="image/svg+xml">
<style>body{{font-family:system-ui,sans-serif;max-width:800px;margin:50px auto;padding:20px;color:#333}}
h1{{color:#1a73e8;display:flex;align-items:center;gap:12px}}.service-logo{{width:52px;height:52px;flex:0 0 auto}}
.ep{{background:#f8f9fa;padding:12px;margin:8px 0;border-left:4px solid #1a73e8;border-radius:4px}}
code{{background:#f4f4f4;padding:2px 6px;border-radius:3px;font-family:monospace}}</style></head>
<body><h1><img class="service-logo" src="{base}/logo.svg?v=5d5f50ea" alt="">SIMKL Service</h1>
<p>Caching proxy for SIMKL trending and DVD release lists.</p>
<h2>Endpoints</h2>
<div class="ep"><strong>GET /stats</strong> — cache status &amp; item counts</div>
<div class="ep"><strong>GET /trending/today/small</strong> — today top-100</div>
<div class="ep"><strong>GET /trending/today/large</strong> — today top-500</div>
<div class="ep"><strong>GET /trending/week/small</strong> — week top-100</div>
<div class="ep"><strong>GET /trending/week/large</strong> — week top-500</div>
<div class="ep"><strong>GET /trending/month/small</strong> — month top-100</div>
<div class="ep"><strong>GET /trending/month/large</strong> — month top-500</div>
<div class="ep"><strong>GET /dvd/small</strong> — DVD releases top-100</div>
<div class="ep"><strong>GET /dvd/large</strong> — DVD releases top-500</div>
<div class="ep"><strong>GET /movies/{{simkl_id}}</strong> — individual movie</div>
<div class="ep"><strong>GET /tv/{{simkl_id}}</strong> — individual TV show</div>
<div class="ep"><strong>GET /anime/{{simkl_id}}</strong> — individual anime</div>
<div class="ep"><strong>GET /movies/find?imdb=&amp;tmdb=&amp;tvdb=</strong></div>
<div class="ep"><strong>GET /tv/find?imdb=&amp;tmdb=&amp;tvdb=</strong></div>
<div class="ep"><strong>GET /anime/find?imdb=&amp;tmdb=&amp;tvdb=&amp;mal=&amp;anilist=&amp;anidb=&amp;kitsu=</strong></div>
<p><a href="{base}/docs">📖 Interactive API docs</a></p>
<p><a href="/">← Back to Home</a></p>
</body></html>"""
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# Trending list endpoints
# ---------------------------------------------------------------------------
@app.get("/trending/today/small")
async def trending_today_small() -> JSONResponse:
    """Return today's top-100 trending list."""
    return await serve_list("trending_today_small")


@app.get("/trending/today/large")
async def trending_today_large() -> JSONResponse:
    """Return today's top-500 trending list."""
    return await serve_list("trending_today_large")


@app.get("/trending/week/small")
async def trending_week_small() -> JSONResponse:
    """Return this week's top-100 trending list."""
    return await serve_list("trending_week_small")


@app.get("/trending/week/large")
async def trending_week_large() -> JSONResponse:
    """Return this week's top-500 trending list."""
    return await serve_list("trending_week_large")


@app.get("/trending/month/small")
async def trending_month_small() -> JSONResponse:
    """Return this month's top-100 trending list."""
    return await serve_list("trending_month_small")


@app.get("/trending/month/large")
async def trending_month_large() -> JSONResponse:
    """Return this month's top-500 trending list."""
    return await serve_list("trending_month_large")


# ---------------------------------------------------------------------------
# DVD list endpoints
# ---------------------------------------------------------------------------
@app.get("/dvd/small")
async def dvd_small() -> JSONResponse:
    """Return DVD releases top-100 list."""
    return await serve_list("dvd_small")


@app.get("/dvd/large")
async def dvd_large() -> JSONResponse:
    """Return DVD releases top-500 list."""
    return await serve_list("dvd_large")


# ---------------------------------------------------------------------------
# Individual item find endpoints (must come before /{simkl_id} routes)
# ---------------------------------------------------------------------------
@app.get("/movies/find")
async def movies_find(
    imdb: Optional[str] = Query(None),  # noqa: B008
    tmdb: Optional[str] = Query(None),  # noqa: B008
    tvdb: Optional[str] = Query(None),  # noqa: B008
) -> JSONResponse:
    """Find a movie by external ID."""
    return await find_item("movies", {"imdb_id": imdb, "tmdb_id": tmdb, "tvdb_id": tvdb})


@app.get("/tv/find")
async def tv_find(
    imdb: Optional[str] = Query(None),  # noqa: B008
    tmdb: Optional[str] = Query(None),  # noqa: B008
    tvdb: Optional[str] = Query(None),  # noqa: B008
) -> JSONResponse:
    """Find a TV show by external ID."""
    return await find_item("tv", {"imdb_id": imdb, "tmdb_id": tmdb, "tvdb_id": tvdb})


@app.get("/anime/find")
async def anime_find(
    imdb: Optional[str] = Query(None),  # noqa: B008
    tmdb: Optional[str] = Query(None),  # noqa: B008
    tvdb: Optional[str] = Query(None),  # noqa: B008
    mal: Optional[str] = Query(None),  # noqa: B008
    anilist: Optional[str] = Query(None),  # noqa: B008
    anidb: Optional[str] = Query(None),  # noqa: B008
    kitsu: Optional[str] = Query(None),  # noqa: B008
) -> JSONResponse:
    """Find an anime by external ID."""
    return await find_item(
        "anime",
        {
            "imdb_id": imdb,
            "tmdb_id": tmdb,
            "tvdb_id": tvdb,
            "mal_id": mal,
            "anilist_id": anilist,
            "anidb_id": anidb,
            "kitsu_id": kitsu,
        },
    )


# ---------------------------------------------------------------------------
# Individual item endpoints
# ---------------------------------------------------------------------------
@app.get("/movies/{simkl_id}")
async def get_movie(simkl_id: int) -> JSONResponse:
    """Return a single cached movie item."""
    return await serve_item("movies", simkl_id)


@app.get("/tv/{simkl_id}")
async def get_tv(simkl_id: int) -> JSONResponse:
    """Return a single cached TV show item."""
    return await serve_item("tv", simkl_id)


@app.get("/anime/{simkl_id}")
async def get_anime(simkl_id: int) -> JSONResponse:
    """Return a single cached anime item."""
    return await serve_item("anime", simkl_id)


# ---------------------------------------------------------------------------
# Stats & health
# ---------------------------------------------------------------------------
@app.get("/stats")
async def stats() -> JSONResponse:
    """Return cache freshness and item counts."""
    lists_status: Dict[str, Any] = {}
    for cfg in LIST_CONFIGS:
        list_file = LISTS_DIR / f"{cfg['stem']}.json"
        fresh = is_fresh(list_file)
        cached_date = None
        if list_file.exists():
            mtime = datetime.fromtimestamp(list_file.stat().st_mtime, tz=timezone.utc).date()
            cached_date = mtime.isoformat()
        lists_status[cfg["key"]] = {"fresh": fresh, "cached_date": cached_date}

    item_counts: Dict[str, int] = {}
    for type_dir in TYPE_DIRS:
        d = DATA_DIR / type_dir
        item_counts[type_dir] = len(list(d.glob("*.json"))) if d.exists() else 0

    queue_size = refresh_queue.qsize() if refresh_queue is not None else 0

    return JSONResponse(
        {
            "status": "online",
            "lists": lists_status,
            "item_counts": item_counts,
            "queue_size": queue_size,
        }
    )


@app.get("/api/health")
@app.get("/health/live")
async def health() -> JSONResponse:
    """Return process liveness without checking storage or SIMKL."""
    return JSONResponse({"status": "ok"})


@app.get("/health/ready")
async def health_ready() -> JSONResponse:
    """Return readiness when local storage and the refresh worker are available."""
    if refresh_queue is None or worker_task is None or worker_task.done():
        return JSONResponse(
            {"status": "not_ready", "reason": "service_initializing"}, status_code=503
        )
    try:
        async with aiosqlite.connect(DATA_DIR / "simkl.db") as db:
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'items'"
            )
            if await cursor.fetchone() is None:
                return JSONResponse(
                    {"status": "not_ready", "reason": "database_initializing"},
                    status_code=503,
                )
    except Exception:
        return JSONResponse(
            {"status": "not_ready", "reason": "database_unavailable"}, status_code=503
        )
    return JSONResponse({"status": "ready"})
