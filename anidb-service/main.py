"""AniDB Mirror Service - FastAPI-based caching service for AniDB anime metadata."""

import asyncio
import ipaddress
import os
import secrets
import time
import xml.etree.ElementTree as ET
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

import aiosqlite
import httpx
from common import extract_seed_data
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import FileResponse, JSONResponse, Response

# --- CONFIG ---
XML_DIR = Path(os.getenv("XML_DIR", "/app/data"))
DB_PATH = Path(os.getenv("DB_PATH", "/app/database/anidb.db"))
SEED_DATA_DIR = Path(os.getenv("SEED_DATA_DIR", "/app/seed_data"))
LOGO_PATH = Path(__file__).resolve().parent / "static" / "anidb-logo.png"
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", "200"))
THROTTLE_SECONDS = int(os.getenv("THROTTLE_SECONDS", "4"))
# Cache lifetime depends on how likely an entry is to change, judged from its start/end dates.
# UPDATE_THRESHOLD is the fallback when an entry's XML can't be parsed.
UPDATE_THRESHOLD = timedelta(days=int(os.getenv("UPDATE_THRESHOLD_DAYS", "14")))
REFRESH_ACTIVE = timedelta(days=int(os.getenv("REFRESH_DAYS_ACTIVE", "14")))  # airing/upcoming
REFRESH_UNKNOWN = timedelta(days=int(os.getenv("REFRESH_DAYS_UNKNOWN", "30")))  # no start date
REFRESH_ENDED_UNDER_1Y = timedelta(days=int(os.getenv("REFRESH_DAYS_ENDED_UNDER_1Y", "30")))
REFRESH_ENDED_1_3Y = timedelta(days=int(os.getenv("REFRESH_DAYS_ENDED_1_3Y", "90")))
REFRESH_ENDED_3_10Y = timedelta(days=int(os.getenv("REFRESH_DAYS_ENDED_3_10Y", "180")))
REFRESH_NOT_FOUND = timedelta(days=int(os.getenv("REFRESH_DAYS_NOT_FOUND", "180")))  # AniDB: no such AID
REFRESH_ENDED_10Y_PLUS = timedelta(days=int(os.getenv("REFRESH_DAYS_ENDED_10Y_PLUS", "365")))
ROOT_PATH = os.getenv("ROOT_PATH", "")  # Set to /anidb-service for path-based routing

# Abuse protection: only lookups that would add a NEW AID to the queue are limited. Cached
# entries and refreshes of cached entries are never limited.
NEW_LOOKUPS_PER_IP = int(os.getenv("NEW_LOOKUPS_PER_IP", "50"))  # per window; 0 disables
NEW_LOOKUP_WINDOW = int(os.getenv("NEW_LOOKUP_WINDOW_SECONDS", "3600"))
NEW_LOOKUPS_PER_IP_DAILY = int(os.getenv("NEW_LOOKUPS_PER_IP_DAILY", "100"))  # per 24h; 0 disables
DAILY_WINDOW = 86400
MAX_QUEUED_NEW = int(os.getenv("MAX_QUEUED_NEW", "5000"))  # 0 disables
MAX_AID = int(os.getenv("MAX_AID", "50000"))  # reject uncached AIDs above this; 0 disables
# Header carrying the client IP; the LAST value is used, since that is the one the nearest
# proxy (Caddy) added. Falls back to the socket peer when absent.
CLIENT_IP_HEADER = os.getenv("CLIENT_IP_HEADER", "X-Forwarded-For")

# Credentials for the operator-only /stats/limits endpoint (disabled if either is unset)
API_USER = os.getenv("API_USER", "")
API_PASS = os.getenv("API_PASS", "")

# AniDB API Configuration
ANIDB_CLIENT = os.getenv("ANIDB_CLIENT", "kometa")
ANIDB_VERSION = os.getenv("ANIDB_VERSION", "1")
ANIDB_PROTO_VER = os.getenv("ANIDB_PROTO_VER", "1")
ANIDB_USERNAME = os.getenv("ANIDB_USERNAME", "")  # For accessing mature content
ANIDB_PASSWORD = os.getenv("ANIDB_PASSWORD", "")  # For accessing mature content

# Global state
# Two lanes: update_queue holds new AIDs (no cached copy); refresh_queue holds stale entries.
# The worker only takes from refresh_queue when update_queue is empty.
update_queue: Optional[asyncio.Queue] = None
refresh_queue: Optional[asyncio.Queue] = None
work_event: Optional[asyncio.Event] = None  # set when either lane gets an item
pending_aids: set = set()  # AIDs waiting in either lane (prevents duplicates)
new_aids: set = set()  # the subset of pending_aids waiting in the new lane
in_flight_aid: Optional[int] = None  # AID the worker is fetching right now
new_lookups: Dict[str, deque] = {}  # client key -> monotonic times of recent new-AID lookups
last_limit_warning: Dict[str, float] = {}  # client key -> monotonic time of last log line
rejected_lookups: Dict[str, int] = {
    "out_of_range": 0,
    "rate_limited": 0,
    "daily_limited": 0,
    "queue_full": 0,
}
worker_task: Optional[asyncio.Task] = None
rate_limit_until: Optional[datetime] = None  # set when AniDB returns 429


async def init_database() -> None:
    """Initialize database with required tables."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS anime (
                aid INTEGER PRIMARY KEY,
                last_updated TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tags (
                aid INTEGER NOT NULL,
                tag_id INTEGER,
                name TEXT NOT NULL,
                weight INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS relations (
                aid INTEGER NOT NULL,
                related_aid INTEGER NOT NULL,
                type TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS api_logs (
                timestamp TEXT NOT NULL,
                aid INTEGER,
                success INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS pending_queue (
                aid INTEGER PRIMARY KEY,
                kind TEXT NOT NULL,
                queued_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS service_state (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_tags_aid ON tags(aid);
            CREATE INDEX IF NOT EXISTS idx_tags_tag_id ON tags(tag_id);
            CREATE INDEX IF NOT EXISTS idx_relations_aid ON relations(aid);
            CREATE INDEX IF NOT EXISTS idx_api_logs_timestamp ON api_logs(timestamp);
        """
        )
        await db.commit()


async def index_xml_to_db(aid: int, xml_text: str) -> None:
    """Parse XML and store metadata in database."""
    try:
        root = ET.fromstring(xml_text)

        async with aiosqlite.connect(DB_PATH) as db:
            # Clear old metadata
            await db.execute("DELETE FROM tags WHERE aid = ?", (aid,))
            await db.execute("DELETE FROM relations WHERE aid = ?", (aid,))

            # Index Tags
            tags = [
                (aid, int(t.get("id") or "0"), t.findtext("name"), int(t.get("weight", 0)))
                for t in root.findall(".//tag")
                if t.findtext("name")
            ]
            if tags:
                await db.executemany("INSERT INTO tags VALUES (?, ?, ?, ?)", tags)

            # Index Relations
            rels = [
                (aid, int(r.get("id") or "0"), r.get("type") or "")
                for r in root.findall(".//relatedanime/anime")
                if r.get("id") and r.get("type")
            ]
            if rels:
                await db.executemany("INSERT INTO relations VALUES (?, ?, ?)", rels)

            # Update Master Record
            await db.execute(
                "INSERT OR REPLACE INTO anime VALUES (?, ?)",
                (aid, datetime.now().isoformat()),
            )
            await db.commit()
    except ET.ParseError as e:
        print(f"❌ XML Parse Error for AID {aid}: {e}")
        raise
    except Exception as e:
        print(f"❌ Database Error for AID {aid}: {e}")
        raise


def parse_anidb_date(value: Optional[str]) -> Optional[datetime]:
    """Parse an AniDB date, which may be partial (YYYY-MM-DD, YYYY-MM or YYYY)."""
    if not value:
        return None
    value = value.strip()
    for fmt, length in (("%Y-%m-%d", 10), ("%Y-%m", 7), ("%Y", 4)):
        try:
            return datetime.strptime(value[:length], fmt)
        except ValueError:
            continue
    return None


def refresh_threshold(xml_text: str, now: Optional[datetime] = None) -> timedelta:
    """Return how long a cached entry stays fresh, based on its airing dates.

    Finished shows rarely change, so they are refreshed far less often than airing or
    upcoming ones. This keeps the daily API budget for entries that actually change.
    """
    now = now or datetime.now()
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return UPDATE_THRESHOLD

    if root.tag == "error":
        return REFRESH_NOT_FOUND  # only "Anime not found" responses are ever cached

    start = parse_anidb_date(root.findtext("startdate"))
    end = parse_anidb_date(root.findtext("enddate"))
    if start is None:
        return REFRESH_UNKNOWN
    if start > now or end is None or end > now:
        return REFRESH_ACTIVE  # upcoming, airing or open-ended

    years_ended = (now - end).days / 365.25
    if years_ended < 1:
        return REFRESH_ENDED_UNDER_1Y
    if years_ended < 3:
        return REFRESH_ENDED_1_3Y
    if years_ended < 10:
        return REFRESH_ENDED_3_10Y
    return REFRESH_ENDED_10Y_PLUS


def cached_xml_path(aid: int) -> Path:
    """Return the existing cache file for an AID, or the path a new one should use.

    Seed data is stored as AnimeDoc_{aid}.xml, live fetches as {aid}.xml. Refreshes
    overwrite whichever exists so an AID never ends up with two copies.
    """
    for name in (f"{aid}.xml", f"AnimeDoc_{aid}.xml"):
        path = XML_DIR / name
        if path.exists():
            return path
    return XML_DIR / f"{aid}.xml"


# The update queue is mirrored in the pending_queue table (best effort) so a restart doesn't
# lose the backlog. kind is "new" (no cached copy) or "refresh" (stale cached copy).
async def persist_pending(aid: int, kind: str) -> None:
    """Record a queued AID, replacing any earlier row for it."""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR REPLACE INTO pending_queue VALUES (?, ?, ?)",
                (aid, kind, datetime.now().isoformat()),
            )
            await db.commit()
    except Exception as e:
        print(f"⚠️ Could not persist queued AID {aid}: {e}")


async def forget_pending(aid: int) -> None:
    """Remove an AID from the persisted queue once it has been handled or dropped."""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM pending_queue WHERE aid = ?", (aid,))
            await db.commit()
    except Exception as e:
        print(f"⚠️ Could not clear queued AID {aid}: {e}")


def client_key(request: Request) -> str:
    """Identify the caller by IP (IPv6 grouped by /64 so one host can't rotate addresses)."""
    raw = None
    if CLIENT_IP_HEADER:
        value = request.headers.get(CLIENT_IP_HEADER)
        if value:
            raw = value.split(",")[-1].strip()
    if not raw and request.client:
        raw = request.client.host
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return raw or "unknown"
    if ip.version == 6:
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def admit_new_lookup(request: Request, aid: int) -> None:
    """Raise unless this caller may add another uncached AID to the queue."""
    if MAX_AID and aid > MAX_AID:
        rejected_lookups["out_of_range"] += 1
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"AID {aid} is out of range."
        )

    if MAX_QUEUED_NEW and len(new_aids) >= MAX_QUEUED_NEW:
        rejected_lookups["queue_full"] += 1
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Fetch queue is full. Try again later.",
            headers={"Retry-After": "3600"},
        )

    if not NEW_LOOKUPS_PER_IP and not NEW_LOOKUPS_PER_IP_DAILY:
        return
    key = client_key(request)
    now = time.monotonic()
    horizon = max(
        NEW_LOOKUP_WINDOW if NEW_LOOKUPS_PER_IP else 0,
        DAILY_WINDOW if NEW_LOOKUPS_PER_IP_DAILY else 0,
    )
    recent = new_lookups.setdefault(key, deque())  # times of lookups this caller had admitted
    while recent and now - recent[0] > horizon:
        recent.popleft()

    # Retry-After is when the entry that currently blocks the caller leaves its window
    if NEW_LOOKUPS_PER_IP_DAILY and len(recent) >= NEW_LOOKUPS_PER_IP_DAILY:
        retry = recent[len(recent) - NEW_LOOKUPS_PER_IP_DAILY] + DAILY_WINDOW - now
        limited, reason, limit = "daily_limited", "daily", f"{NEW_LOOKUPS_PER_IP_DAILY}/day"
    else:
        in_window = [t for t in recent if now - t <= NEW_LOOKUP_WINDOW]
        if NEW_LOOKUPS_PER_IP and len(in_window) >= NEW_LOOKUPS_PER_IP:
            retry = in_window[len(in_window) - NEW_LOOKUPS_PER_IP] + NEW_LOOKUP_WINDOW - now
            limited, reason = "rate_limited", "hourly"
            limit = f"{NEW_LOOKUPS_PER_IP}/{NEW_LOOKUP_WINDOW}s"
        else:
            limited = None

    if limited:
        rejected_lookups[limited] += 1
        if now - last_limit_warning.get(key, -horizon - 1) > NEW_LOOKUP_WINDOW:  # once per window
            last_limit_warning[key] = now
            print(f"🚧 {key} hit the {reason} new-lookup limit ({limit})")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many lookups for uncached anime. Try again later.",
            headers={"Retry-After": str(max(int(retry) + 1, 1))},
        )
    recent.append(now)

    if len(new_lookups) > 10000:  # bound memory: drop callers with nothing recent
        for stale_key in [k for k, v in new_lookups.items() if not v or now - v[-1] > horizon]:
            del new_lookups[stale_key]
            last_limit_warning.pop(stale_key, None)


def is_queued(aid: int) -> bool:
    """True if the AID is waiting in either lane or is being fetched right now."""
    return aid in pending_aids or aid == in_flight_aid


async def enqueue_aid(aid: int, is_new: bool) -> None:
    """Queue an AID for fetching and persist it."""
    pending_aids.add(aid)
    if is_new:
        new_aids.add(aid)
        await update_queue.put(aid)
    else:
        await refresh_queue.put(aid)
    if work_event is not None:
        work_event.set()
    await persist_pending(aid, "new" if is_new else "refresh")


async def save_rate_limit(until: Optional[datetime]) -> None:
    """Persist (or clear) the 429 back-off so a restart doesn't reset it."""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            if until is None:
                await db.execute("DELETE FROM service_state WHERE key = 'rate_limit_until'")
            else:
                await db.execute(
                    "INSERT OR REPLACE INTO service_state VALUES ('rate_limit_until', ?)",
                    (until.isoformat(),),
                )
            await db.commit()
    except Exception as e:
        print(f"⚠️ Could not persist rate limit state: {e}")


async def restore_queue() -> None:
    """Reload the persisted backlog (uncached AIDs first) and any active 429 back-off."""
    global rate_limit_until
    pending_aids.clear()
    new_aids.clear()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT aid, kind FROM pending_queue ORDER BY kind = 'new' DESC, queued_at"
        )
        rows = await cursor.fetchall()
        cursor = await db.execute("SELECT value FROM service_state WHERE key = 'rate_limit_until'")
        state = await cursor.fetchone()

    for aid, kind in rows:
        pending_aids.add(aid)
        if kind == "new":
            new_aids.add(aid)
            update_queue.put_nowait(aid)
        else:
            refresh_queue.put_nowait(aid)
    if rows:
        print(f"♻️ Restored {len(rows)} queued AIDs ({len(new_aids)} new)")
    if work_event is not None and rows:
        work_event.set()

    if state:
        until = datetime.fromisoformat(state[0])
        if until > datetime.now():
            rate_limit_until = until
            print(f"⏸️ Restored AniDB back-off until {until.isoformat()}")


async def check_daily_limit() -> bool:
    """Check if we've hit the daily API request limit."""
    # Ensure DB directory exists
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(DB_PATH) as db:
        cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
        cursor = await db.execute("SELECT COUNT(*) FROM api_logs WHERE timestamp > ?", (cutoff,))
        result = await cursor.fetchone()
        count = result[0] if result else 0
        return count < DAILY_LIMIT


async def log_api_request(aid: int, success: bool = True) -> None:
    """Log API request for rate limiting tracking."""
    # Ensure DB directory exists
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO api_logs VALUES (?, ?, ?)",
            (datetime.now().isoformat(), aid, 1 if success else 0),
        )
        await db.commit()


def filter_mature_content(xml_text: str) -> str:
    """Remove mature content elements from XML response."""
    try:
        root = ET.fromstring(xml_text)

        # Remove mature tags (18+ restricted content)
        tags_to_remove = root.findall(".//tag[name='18 restricted']")
        for tag in tags_to_remove:
            parent = root.find(".//tag[name='18 restricted']/..")
            if parent is not None:
                parent.remove(tag)

        # Remove mature categories
        mature_keywords = ["hentai", "pornography", "18 restricted", "adult"]
        categories_parent = root.find(".//categories")
        if categories_parent is not None:
            for category in list(categories_parent.findall("category")):
                name = category.findtext("name", "").lower()
                if any(keyword in name for keyword in mature_keywords):
                    categories_parent.remove(category)

        return ET.tostring(root, encoding="unicode")
    except Exception as e:
        print(f"⚠️ Error filtering mature content: {e}")
        return xml_text  # Return original if filtering fails


async def fetch_from_anidb(aid: int) -> str:
    """Fetch anime metadata from AniDB API with proper throttling."""
    if not await check_daily_limit():
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Daily API limit reached. Try again tomorrow.",
        )

    url = "http://api.anidb.net:9001/httpapi"
    params = {
        "request": "anime",
        "client": ANIDB_CLIENT,
        "clientver": ANIDB_VERSION,
        "protover": ANIDB_PROTO_VER,
        "aid": aid,
    }

    # Add authentication to access mature content
    if ANIDB_USERNAME and ANIDB_PASSWORD:
        params["user"] = ANIDB_USERNAME
        params["pass"] = ANIDB_PASSWORD

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()

            # Check for AniDB error responses
            if "banned" in response.text.lower():
                await log_api_request(aid, success=False)
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="AniDB API access temporarily banned",
                )

            # AniDB reports problems as a bare <error> document. "Anime not found" is a real
            # answer and is cached (with a long lifetime); anything else (bad client version,
            # server trouble, ...) must not be stored as if it were anime data.
            body = response.text.strip()
            if body.startswith("<error") and "not found" not in body.lower():
                await log_api_request(aid, success=False)
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=f"AniDB returned an error: {body[:200]}",
                )

            await log_api_request(aid, success=True)
            return str(response.text)

    except httpx.HTTPStatusError as e:
        await log_api_request(aid, success=False)
        if e.response.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="AniDB rate limit (429) — backing off for 24 hours",
            )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"AniDB API error: {str(e)}",
        )
    except httpx.HTTPError as e:
        await log_api_request(aid, success=False)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"AniDB API error: {str(e)}",
        )


async def next_queued_aid() -> tuple:
    """Wait for work and return (aid, is_new, queue); new AIDs always come before refreshes."""
    global work_event
    if work_event is None:
        work_event = asyncio.Event()
    while True:
        if update_queue is not None and not update_queue.empty():
            return update_queue.get_nowait(), True, update_queue
        if refresh_queue is not None and not refresh_queue.empty():
            return refresh_queue.get_nowait(), False, refresh_queue
        work_event.clear()
        await work_event.wait()


async def anidb_worker() -> None:
    """Background worker: drains new AIDs first, then refreshes, with throttling."""
    global rate_limit_until, work_event, in_flight_aid
    work_event = asyncio.Event()  # bound to this worker's event loop
    print("🚀 AniDB worker started")

    while True:
        aid = 0
        is_new = False
        source: Optional[asyncio.Queue] = None
        try:
            # Honour any active 429 back-off before pulling from the queue
            if rate_limit_until is not None:
                delay = (rate_limit_until - datetime.now()).total_seconds()
                if delay > 0:
                    print(
                        f"⏸️ AniDB rate-limited — pausing worker for "
                        f"{delay / 3600:.1f}h (until {rate_limit_until.isoformat()})"
                    )
                    await asyncio.sleep(delay)
                rate_limit_until = None
                await save_rate_limit(None)

            aid, is_new, source = await next_queued_aid()

            was_pending = aid in pending_aids
            pending_aids.discard(aid)
            new_aids.discard(aid)

            # A refresh whose AID was since promoted to the new lane has already been
            # handled there (it is no longer pending), so skip the leftover copy
            if not is_new and not was_pending:
                source.task_done()
                continue

            in_flight_aid = aid
            print(f"⏳ Processing AID {aid}...")

            # Fetch from AniDB
            xml_text = await fetch_from_anidb(aid)

            # Save to file
            cached_xml_path(aid).write_text(xml_text, encoding="utf-8")

            # Index to database
            await index_xml_to_db(aid, xml_text)

            await forget_pending(aid)
            print(f"✅ Cached AID {aid}")

            # Mandatory throttle
            await asyncio.sleep(THROTTLE_SECONDS)

            source.task_done()
        except asyncio.CancelledError:
            # Worker is being shut down, don't call task_done
            break
        except Exception as e:
            if isinstance(e, HTTPException) and e.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
                rate_limit_until = datetime.now() + timedelta(hours=24)
                print(f"🚫 AniDB 429 — suspending requests until {rate_limit_until.isoformat()}")
                await save_rate_limit(rate_limit_until)
                if aid:
                    await enqueue_aid(aid, is_new)
            else:
                print(f"❌ Worker error for AID {aid}: {e}")
                if aid:
                    await forget_pending(aid)
            if source is not None:
                source.task_done()
        finally:
            in_flight_aid = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage FastAPI lifespan context for startup/shutdown."""
    global worker_task, update_queue, refresh_queue

    # Startup
    print("🔧 Initializing AniDB Service...")
    XML_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Create the queue in this event loop
    update_queue = asyncio.Queue()
    refresh_queue = asyncio.Queue()

    # Set startup flag for healthcheck
    app.state.starting_up = True

    # Extract seed data if data directory is empty
    extract_seed_data(XML_DIR, SEED_DATA_DIR)

    # Initialize database
    await init_database()
    await restore_queue()

    # Start background indexing if database is empty
    async def index_seed_data_background():
        """Index seed data in background without blocking startup."""
        try:
            import gc

            import aiosqlite

            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute("SELECT COUNT(*) FROM anime")
                result = await cursor.fetchone()
                count = result[0] if result else 0

                if count == 0 and XML_DIR.exists():
                    xml_files = list(XML_DIR.glob("*.xml"))
                    if xml_files:
                        print(f"📚 Indexing {len(xml_files)} seed files in background...")
                        indexed_count = 0
                        for xml_file in xml_files:
                            try:
                                # Handle both formats: "123.xml" and "AnimeDoc_123.xml"
                                if "_" in xml_file.stem:
                                    aid = xml_file.stem.split("_")[1]
                                else:
                                    aid = xml_file.stem

                                xml_text = xml_file.read_text(encoding="utf-8")
                                await index_xml_to_db(int(aid), xml_text)
                                indexed_count += 1

                                # Commit every 100 files to reduce memory pressure
                                if indexed_count % 100 == 0:
                                    await db.commit()
                                    if len(xml_files) > 100:
                                        print(
                                            f"   Progress: {indexed_count}/{len(xml_files)} files indexed..."
                                        )
                                    gc.collect()
                            except Exception as e:
                                print(f"⚠️ Error indexing {xml_file.name}: {e}")
                        await db.commit()
                        print(f"✅ Indexed {indexed_count} files")
        except Exception as e:
            print(f"❌ Background indexing failed: {e}")

    # Start background tasks
    index_task = asyncio.create_task(index_seed_data_background())
    worker_task = asyncio.create_task(anidb_worker())

    # Service is ready immediately
    app.state.starting_up = False
    print("✅ Service ready (seed indexing in background if needed)")

    yield

    # Shutdown
    print("🛑 Shutting down...")
    tasks = [t for t in (index_task, worker_task) if t]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(
    title="AniDB Mirror Service",
    lifespan=lifespan,
    root_path=ROOT_PATH,
    openapi_url="/openapi.json" if ROOT_PATH else "/openapi.json",
    docs_url="/docs" if ROOT_PATH else "/docs",
    redoc_url="/redoc" if ROOT_PATH else "/redoc",
    redirect_slashes=False,
)


@app.get("/health/live")
async def health_live() -> Dict[str, str]:
    """Return process liveness without checking storage or AniDB."""
    return {"status": "ok"}


@app.get("/health/ready")
async def health_ready() -> JSONResponse:
    """Return readiness when local storage and the fetch worker are available."""
    if (
        getattr(app.state, "starting_up", True)
        or update_queue is None
        or worker_task is None
        or worker_task.done()
    ):
        return JSONResponse(
            {"status": "not_ready", "reason": "service_initializing"}, status_code=503
        )
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            tables = {row[0] for row in await cursor.fetchall()}
        if not {"anime", "tags", "relations", "api_logs"}.issubset(tables):
            return JSONResponse(
                {"status": "not_ready", "reason": "database_initializing"}, status_code=503
            )
    except Exception:
        return JSONResponse(
            {"status": "not_ready", "reason": "database_unavailable"}, status_code=503
        )
    return JSONResponse({"status": "ready"})


@app.get("/logo.png", include_in_schema=False)
async def logo() -> FileResponse:
    """Return the AniDB service logo."""
    return FileResponse(
        LOGO_PATH,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/")
async def root(request: Request):
    """Root endpoint with API information."""
    from fastapi.responses import HTMLResponse

    # Construct the base URL from the request
    base_url = f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"
    if ROOT_PATH:
        base_url += ROOT_PATH

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>AniDB Mirror Service</title>
        <link rel="icon" href="{base_url}/logo.png" type="image/png">
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                max-width: 800px;
                margin: 50px auto;
                padding: 20px;
                line-height: 1.6;
                color: #333;
            }}
            h1 {{ color: #2c3e50; display: flex; align-items: center; gap: 12px; }}
            .service-logo {{ width: 64px; height: 64px; object-fit: contain; flex: 0 0 auto; }}
            code {{
                background: #f4f4f4;
                padding: 2px 6px;
                border-radius: 3px;
                font-family: 'Courier New', monospace;
            }}
            .endpoint {{
                background: #f8f9fa;
                padding: 15px;
                margin: 10px 0;
                border-left: 4px solid #007bff;
                border-radius: 4px;
            }}
            a {{ color: #007bff; text-decoration: none; }}
            a:hover {{ text-decoration: underline; }}
        </style>
    </head>
    <body>
        <h1><img class="service-logo" src="{base_url}/logo.png" alt="">AniDB Mirror Service</h1>
        <p>A caching service for AniDB anime metadata with rate limiting and background updates.</p>

        <p>NOTE:</p>
        <p>This is not a complete mirror of AniDB and is not intended to be.  It is a cache of IDs requested by Kometa users.</p>
        <p>When an ID is requested, the existing data is returned and depending on a variety of thresholds a refresh of the data might be queued.  Note that this means the data retrieved from this service may be somewhat out of date.  This is unavoidable due to limitations on the AniDB API and the wish not to get this service banned.</p>
        <p>If a new ID unknown to the cache is requested, that new ID is queued for retrieval.  New IDs take precedence over refreshing existing IDs.</p>
        <p>There are throttles on how many IDs a given IP can request daily.</p>

        <h2>API Endpoints</h2>

        <div class="endpoint">
            <strong>GET /stats</strong> - Service statistics<br>
            <code>curl {base_url}/stats</code>
        </div>

        <div class="endpoint">
            <strong>GET /anime/{{aid}}</strong> - Get anime by AniDB ID<br>
            <code>curl {base_url}/anime/1</code>
        </div>

        <div class="endpoint">
            <strong>GET /tags</strong> - List all tags with usage statistics<br>
            <code>curl {base_url}/tags</code>
        </div>

        <div class="endpoint">
            <strong>GET /search/tags</strong> - Search by tags<br>
            <code>curl "{base_url}/search/tags?tags=action,comedy&min_weight=300&mature=true"</code>
        </div>

        <div class="endpoint">
            <strong>GET /tags/{{tag_id}}</strong> - Get anime by tag ID<br>
            <code>curl "{base_url}/tags/36?limit=10"</code>
        </div>

        <h2>API Documentation</h2>

        <div class="endpoint">
            <strong><a href="{base_url}/docs">Swagger UI</a></strong> - Interactive API documentation<br>
            Try out endpoints directly from your browser
        </div>

        <div class="endpoint">
            <strong><a href="{base_url}/redoc">ReDoc</a></strong> - Alternative API documentation<br>
            Clean, readable documentation format
        </div>

        <div class="endpoint">
            <strong><a href="{base_url}/openapi.json">OpenAPI Schema</a></strong> - Machine-readable API specification<br>
            JSON schema for automated tools and clients
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


@app.get("/tags")
async def list_tags():
    """List all known tags with usage statistics."""
    from fastapi.responses import HTMLResponse

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                """
                SELECT name, COUNT(DISTINCT aid) as anime_count, AVG(weight) as avg_weight
                FROM tags
                GROUP BY LOWER(name)
                ORDER BY anime_count DESC, avg_weight DESC
            """
            )
            tags = await cursor.fetchall()

        tag_rows = ""
        for name, count, avg_weight in tags:
            tag_rows += f"""
                <tr>
                    <td>{name}</td>
                    <td>{count}</td>
                    <td>{int(avg_weight) if avg_weight else 0}</td>
                </tr>
            """

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>AniDB Tags - AniDB Mirror Service</title>
            <link rel="icon" href="{ROOT_PATH}/logo.png" type="image/png">
            <style>
                body {{
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                    max-width: 1200px;
                    margin: 50px auto;
                    padding: 20px;
                    line-height: 1.6;
                    color: #333;
                }}
                h1 {{ color: #2c3e50; display: flex; align-items: center; gap: 12px; }}
                .service-logo {{ width: 48px; height: 48px; object-fit: contain; flex: 0 0 auto; }}
                table {{
                    width: 100%;
                    border-collapse: collapse;
                    margin-top: 20px;
                    background: white;
                    box-shadow: 0 2px 4px rgba(0,0,0,0.1);
                }}
                th {{
                    background: #007bff;
                    color: white;
                    padding: 12px;
                    text-align: left;
                    position: sticky;
                    top: 0;
                }}
                td {{
                    padding: 10px 12px;
                    border-bottom: 1px solid #eee;
                }}
                tr:hover {{
                    background: #f8f9fa;
                }}
                .stats {{
                    background: #f8f9fa;
                    padding: 15px;
                    border-radius: 5px;
                    margin-bottom: 20px;
                }}
                a {{ color: #007bff; text-decoration: none; }}
                a:hover {{ text-decoration: underline; }}
            </style>
        </head>
        <body>
            <h1><img class="service-logo" src="{ROOT_PATH}/logo.png" alt="">All Tags</h1>
            <p><a href="{ROOT_PATH}/">← Back to Home</a></p>

            <div class="stats">
                <strong>Total unique tags:</strong> {len(tags)}
            </div>

            <table>
                <thead>
                    <tr>
                        <th>Tag Name</th>
                        <th>Anime Count</th>
                        <th>Avg Weight</th>
                    </tr>
                </thead>
                <tbody>
                    {tag_rows}
                </tbody>
            </table>
        </body>
        </html>
        """
        return HTMLResponse(content=html_content)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Database error: {str(e)}"
        )


@app.get("/stats")
async def get_stats() -> Dict[str, Any]:
    """Public health check endpoint for monitoring."""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM anime")
            row = await cursor.fetchone()
            total = row[0] if row else 0

            cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
            cursor = await db.execute(
                "SELECT COUNT(*) FROM api_logs WHERE timestamp > ?", (cutoff,)
            )
            row = await cursor.fetchone()
            daily = row[0] if row else 0

            cursor = await db.execute("SELECT kind, COUNT(*) FROM pending_queue GROUP BY kind")
            queued = dict(await cursor.fetchall())

        return {
            "status": "online",
            "cached_anime": total,
            "api_calls_last_24h": daily,
            "queue_size": update_queue.qsize() + (refresh_queue.qsize() if refresh_queue else 0),
            "queued_new": queued.get("new", 0),
            "queued_refresh": queued.get("refresh", 0),
            "rejected_new_lookups": sum(rejected_lookups.values()),  # since last restart
            "daily_limit": DAILY_LIMIT,
            "rate_limit_until": rate_limit_until.isoformat() if rate_limit_until else None,
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database error: {str(e)}",
        )


basic_auth = HTTPBasic(auto_error=False)


@app.get("/stats/limits", include_in_schema=False)
async def get_limit_stats(credentials: Optional[HTTPBasicCredentials] = Depends(basic_auth)):
    """Operator view of why lookups were rejected. Not public, so probes can't read it."""
    if not API_USER or not API_PASS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    # Compare as bytes: compare_digest rejects non-ASCII str
    valid = (
        credentials is not None
        and secrets.compare_digest(credentials.username.encode(), API_USER.encode())
        & secrets.compare_digest(credentials.password.encode(), API_PASS.encode())
    )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )
    return {
        "rejected_new_lookups": dict(rejected_lookups),  # since last restart
        "tracked_ips": len(new_lookups),
    }


@app.get("/anime/{aid}")
async def get_anime(aid: int, request: Request, mature: bool = False) -> Response:
    """
    Fetch anime metadata by AniDB ID.

    Returns cached XML if available and fresh, otherwise queues update.

    Args:
        aid: AniDB anime ID
        mature: Include mature/18+ content (default: False)
    """
    if aid <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid AID. Must be a positive integer.",
        )

    # Check for both naming formats: {aid}.xml and AnimeDoc_{aid}.xml
    xml_file = cached_xml_path(aid)

    # Check if cached and fresh
    if xml_file.exists():
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute("SELECT last_updated FROM anime WHERE aid = ?", (aid,))
                row = await cursor.fetchone()

                if row:
                    last_updated = datetime.fromisoformat(row[0])
                    age = datetime.now() - last_updated
                    content = xml_file.read_text(encoding="utf-8")
                    threshold = refresh_threshold(content)

                    if age < threshold:
                        # Serve from cache; filter mature content if requested
                        if not mature:
                            content = filter_mature_content(content)

                        return Response(
                            content=content,
                            media_type="application/xml",
                            headers={
                                "X-Cache": "HIT",
                                "X-Age-Days": str(age.days),
                                "X-Refresh-After-Days": str(threshold.days),
                                "X-Mature-Filter": "disabled" if mature else "enabled",
                            },
                        )
                    else:
                        # Cache exists but is stale - queue for update and return stale content
                        if not is_queued(aid):
                            await enqueue_aid(aid, False)

                        if not mature:
                            content = filter_mature_content(content)

                        return Response(
                            content=content,
                            media_type="application/xml",
                            headers={
                                "X-Cache": "STALE",
                                "X-Status": "Refreshing",
                                "X-Mature-Filter": "disabled" if mature else "enabled",
                                "X-Age-Days": str(age.days),
                                "X-Refresh-After-Days": str(threshold.days),
                            },
                        )
                else:
                    # File exists but no DB entry - treat as stale
                    if not is_queued(aid):
                        await enqueue_aid(aid, False)

                    content = xml_file.read_text(encoding="utf-8")
                    if not mature:
                        content = filter_mature_content(content)

                    return Response(
                        content=content,
                        media_type="application/xml",
                        headers={
                            "X-Cache": "STALE",
                            "X-Status": "Refreshing",
                            "X-Mature-Filter": "disabled" if mature else "enabled",
                        },
                    )
        except Exception as e:
            print(f"⚠️ Cache check error for AID {aid}: {e}")

    # Queue for update if not in cache
    if not is_queued(aid):
        admit_new_lookup(request, aid)
        await enqueue_aid(aid, True)
    elif aid in pending_aids and aid not in new_aids:
        # Queued as a refresh but the cached copy is gone: also queue it as new. The worker
        # takes the new-lane entry first and skips the leftover refresh entry.
        new_aids.add(aid)
        await update_queue.put(aid)
        if work_event is not None:
            work_event.set()
        await persist_pending(aid, "new")

    # No cache available
    raise HTTPException(
        status_code=status.HTTP_202_ACCEPTED,
        detail=f"AID {aid} queued for fetching. Check back in a few moments.",
    )


@app.get("/search/tags")
async def search_by_tags(tags: str, min_weight: int = 200, mature: bool = False) -> Dict[str, Any]:
    """
    Search for anime by tags.

    Example: /search/tags?tags=action,comedy&min_weight=300&mature=true

    Args:
        tags: Comma-separated list of tags to search for
        min_weight: Minimum tag weight (default: 200)
        mature: Include mature/18+ content (default: False)
    """
    tag_list = [t.strip().lower() for t in tags.split(",")]

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            placeholders = ",".join("?" * len(tag_list))

            # Build query with optional mature content exclusion
            if mature:
                query = f"""
                    SELECT aid, COUNT(*) as match_count
                    FROM tags
                    WHERE LOWER(name) IN ({placeholders})
                    AND weight >= ?
                    GROUP BY aid
                    ORDER BY match_count DESC
                    LIMIT 100
                """
                cursor = await db.execute(query, (*tag_list, min_weight))
            else:
                # Exclude anime with mature tags
                mature_keywords = ["hentai", "pornography", "18 restricted", "adult"]
                mature_placeholders = ",".join("?" * len(mature_keywords))
                query = f"""
                    SELECT aid, COUNT(*) as match_count
                    FROM tags
                    WHERE LOWER(name) IN ({placeholders})
                    AND weight >= ?
                    AND aid NOT IN (
                        SELECT DISTINCT aid
                        FROM tags
                        WHERE LOWER(name) IN ({mature_placeholders})
                    )
                    GROUP BY aid
                    ORDER BY match_count DESC
                    LIMIT 100
                """
                cursor = await db.execute(query, (*tag_list, min_weight, *mature_keywords))

            results = await cursor.fetchall()

        return {
            "query": tag_list,
            "min_weight": min_weight,
            "mature": mature,
            "results": [{"aid": aid, "tag_matches": count} for aid, count in results],
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Search error: {str(e)}",
        )


@app.get("/tags/{tag_id}")
async def get_anime_by_tag(tag_id: int, limit: int = 100, mature: bool = False) -> Dict[str, Any]:
    """
    Get anime by tag ID.

    Example: /tags/36?limit=10

    Args:
        tag_id: The AniDB tag ID
        limit: Maximum number of results to return (default: 100, max: 1000)
        mature: Include mature/18+ content (default: False)
    """
    if tag_id <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid tag_id. Must be a positive integer.",
        )

    if limit <= 0 or limit > 1000:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Limit must be between 1 and 1000.",
        )

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            # First get the tag name
            cursor = await db.execute(
                "SELECT DISTINCT name FROM tags WHERE tag_id = ? LIMIT 1", (tag_id,)
            )
            tag_row = await cursor.fetchone()
            tag_name = tag_row[0] if tag_row else None

            # Build query with optional mature content exclusion
            if mature:
                query = """
                    SELECT aid, weight
                    FROM tags
                    WHERE tag_id = ?
                    ORDER BY weight DESC
                    LIMIT ?
                """
                cursor = await db.execute(query, (tag_id, limit))
            else:
                # Exclude anime with mature tags
                mature_keywords = ["hentai", "pornography", "18 restricted", "adult"]
                mature_placeholders = ",".join("?" * len(mature_keywords))
                query = f"""
                    SELECT aid, weight
                    FROM tags
                    WHERE tag_id = ?
                    AND aid NOT IN (
                        SELECT DISTINCT aid
                        FROM tags
                        WHERE LOWER(name) IN ({mature_placeholders})
                    )
                    ORDER BY weight DESC
                    LIMIT ?
                """
                cursor = await db.execute(query, (tag_id, *mature_keywords, limit))

            results = await cursor.fetchall()

        return {
            "tag_id": tag_id,
            "tag_name": tag_name,
            "limit": limit,
            "mature": mature,
            "count": len(results),
            "results": [{"aid": aid, "weight": weight} for aid, weight in results],
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Search error: {str(e)}",
        )
