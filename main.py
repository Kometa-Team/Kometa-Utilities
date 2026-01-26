import asyncio
import os
import secrets
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

import aiosqlite
import httpx
from fastapi import Depends, FastAPI, HTTPException, status, Request
from fastapi.responses import Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from common import extract_seed_data

# --- CONFIG ---
XML_DIR = Path(os.getenv("XML_DIR", "/app/data"))
DB_PATH = Path(os.getenv("DB_PATH", "/app/database/anidb.db"))
SEED_DATA_DIR = Path(os.getenv("SEED_DATA_DIR", "/app/seed_data"))
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", "200"))
THROTTLE_SECONDS = int(os.getenv("THROTTLE_SECONDS", "4"))
UPDATE_THRESHOLD = timedelta(days=int(os.getenv("UPDATE_THRESHOLD_DAYS", "14")))
ROOT_PATH = os.getenv("ROOT_PATH", "")  # Set to /anidb-service for path-based routing

# AniDB API Configuration
ANIDB_CLIENT = os.getenv("ANIDB_CLIENT", "kometa")
ANIDB_VERSION = os.getenv("ANIDB_VERSION", "1")
ANIDB_PROTO_VER = os.getenv("ANIDB_PROTO_VER", "1")
ANIDB_USERNAME = os.getenv("ANIDB_USERNAME", "")  # For accessing mature content
ANIDB_PASSWORD = os.getenv("ANIDB_PASSWORD", "")  # For accessing mature content

# Authentication
API_USER = os.getenv("API_USER", "kometa_admin")
API_PASS = os.getenv("API_PASS", "change_me_to_something_secure")

# Global state
security = HTTPBasic()
update_queue: Optional[asyncio.Queue] = None
pending_aids: set = set()
worker_task: Optional[asyncio.Task] = None


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
            CREATE INDEX IF NOT EXISTS idx_tags_aid ON tags(aid);
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
                (aid, t.findtext("name"), int(t.get("weight", 0)))
                for t in root.findall(".//tag")
                if t.findtext("name")
            ]
            if tags:
                await db.executemany("INSERT INTO tags VALUES (?, ?, ?)", tags)

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


async def check_daily_limit() -> bool:
    """Check if we've hit the daily API request limit."""
    # Ensure DB directory exists
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(DB_PATH) as db:
        cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
        cursor = await db.execute(
            "SELECT COUNT(*) FROM api_logs WHERE timestamp > ?", (cutoff,)
        )
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

            await log_api_request(aid, success=True)
            return response.text

    except httpx.HTTPError as e:
        await log_api_request(aid, success=False)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"AniDB API error: {str(e)}",
        )


async def anidb_worker() -> None:
    """Background worker that processes the update queue with throttling."""
    print("🚀 AniDB worker started")

    while True:
        aid = 0
        try:
            aid = await update_queue.get()

            if aid in pending_aids:
                pending_aids.remove(aid)

            print(f"⏳ Processing AID {aid}...")

            # Fetch from AniDB
            xml_text = await fetch_from_anidb(aid)

            # Save to file
            xml_file = XML_DIR / f"{aid}.xml"
            xml_file.write_text(xml_text, encoding="utf-8")

            # Index to database
            await index_xml_to_db(aid, xml_text)

            print(f"✅ Cached AID {aid}")

            # Mandatory throttle
            await asyncio.sleep(THROTTLE_SECONDS)

            update_queue.task_done()
        except asyncio.CancelledError:
            # Worker is being shut down, don't call task_done
            break
        except Exception as e:
            print(f"❌ Worker error for AID {aid}: {e}")
            update_queue.task_done()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan context for startup/shutdown."""
    global worker_task, update_queue

    # Startup
    print("🔧 Initializing AniDB Service...")
    XML_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    
    # Create the queue in this event loop
    update_queue = asyncio.Queue()

    # Extract seed data if data directory is empty
    extract_seed_data(XML_DIR, SEED_DATA_DIR)

    # Initialize database
    await init_database()

    # Index seed data if database is empty
    import aiosqlite

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM anime")
        result = await cursor.fetchone()
        count = result[0] if result else 0

        if count == 0 and XML_DIR.exists():
            xml_files = list(XML_DIR.glob("*.xml"))
            if xml_files:
                print(f"📚 Indexing {len(xml_files)} seed files...")
                indexed_count = 0
                for xml_file in xml_files:
                    try:
                        aid = xml_file.stem.split("_")[
                            1
                        ]  # Get filename without extension

                        xml_text = xml_file.read_text(encoding="utf-8")
                        await index_xml_to_db(int(aid), xml_text)
                        indexed_count += 1
                        if len(xml_files) > 100 and indexed_count % 1000 == 0:
                            print(
                                f"   Progress: {indexed_count}/{len(xml_files)} files indexed..."
                            )
                    except Exception as e:
                        print(f"⚠️ Error indexing {xml_file.name}: {e}")
                print(f"✅ Indexed {indexed_count} files")

    worker_task = asyncio.create_task(anidb_worker())
    print("✅ Service ready")

    yield

    # Shutdown
    print("🛑 Shutting down...")
    if worker_task:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="AniDB Mirror Service",
    lifespan=lifespan,
    root_path=ROOT_PATH,
    openapi_url="/openapi.json" if ROOT_PATH else "/openapi.json",
    docs_url="/docs" if ROOT_PATH else "/docs",
    redoc_url="/redoc" if ROOT_PATH else "/redoc",
)


def authenticate(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    """Verify HTTP Basic Authentication credentials."""
    is_user_ok = secrets.compare_digest(credentials.username, API_USER)
    is_pass_ok = secrets.compare_digest(credentials.password, API_PASS)
    if not (is_user_ok and is_pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


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
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                max-width: 800px;
                margin: 50px auto;
                padding: 20px;
                line-height: 1.6;
                color: #333;
            }}
            h1 {{ color: #2c3e50; }}
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
        <h1>🎬 AniDB Mirror Service</h1>
        <p>A caching service for AniDB anime metadata with rate limiting and background updates.</p>
        
        <h2>API Endpoints</h2>
        
        <div class="endpoint">
            <strong>GET /stats</strong> - Service statistics (no auth required)<br>
            <code>curl {base_url}/stats</code>
        </div>
        
        <div class="endpoint">
            <strong>GET /anime/{{aid}}</strong> - Get anime by AniDB ID (requires auth)<br>
            <code>curl -u username:password {base_url}/anime/1</code>
        </div>
        
        <div class="endpoint">
            <strong>GET /search/tags</strong> - Search by tags (requires auth)<br>
            <code>curl -u username:password "{base_url}/search/tags?tags=action,comedy&min_weight=300"</code>
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
            cursor = await db.execute("""
                SELECT name, COUNT(DISTINCT aid) as anime_count, AVG(weight) as avg_weight
                FROM tags
                GROUP BY LOWER(name)
                ORDER BY anime_count DESC, avg_weight DESC
            """)
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
            <style>
                body {{
                    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                    max-width: 1200px;
                    margin: 50px auto;
                    padding: 20px;
                    line-height: 1.6;
                    color: #333;
                }}
                h1 {{ color: #2c3e50; }}
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
            <h1>🏷️ All Tags</h1>
            <p><a href="/">← Back to Home</a></p>
            
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
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database error: {{str(e)}}"
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

        return {
            "status": "online",
            "cached_anime": total,
            "api_calls_last_24h": daily,
            "queue_size": update_queue.qsize(),
            "daily_limit": DAILY_LIMIT,
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database error: {str(e)}",
        )


@app.get("/anime/{aid}", dependencies=[Depends(authenticate)])
async def get_anime(aid: int, mature: bool = True) -> Response:
    """
    Fetch anime metadata by AniDB ID.
    Returns cached XML if available and fresh, otherwise queues update.

    Args:
        aid: AniDB anime ID
        mature: Include mature/18+ content (default: True)
    """
    if aid <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid AID. Must be a positive integer.",
        )

    # Check for both naming formats: {aid}.xml and AnimeDoc_{aid}.xml
    xml_file = XML_DIR / f"{aid}.xml"
    if not xml_file.exists():
        xml_file = XML_DIR / f"AnimeDoc_{aid}.xml"

    # Check if cached and fresh
    if xml_file.exists():
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    "SELECT last_updated FROM anime WHERE aid = ?", (aid,)
                )
                row = await cursor.fetchone()

                if row:
                    last_updated = datetime.fromisoformat(row[0])
                    age = datetime.now() - last_updated

                    if age < UPDATE_THRESHOLD:
                        # Serve from cache
                        content = xml_file.read_text(encoding="utf-8")

                        # Filter mature content if requested
                        if not mature:
                            content = filter_mature_content(content)

                        return Response(
                            content=content,
                            media_type="application/xml",
                            headers={
                                "X-Cache": "HIT",
                                "X-Age-Days": str(age.days),
                                "X-Mature-Filter": "disabled" if mature else "enabled",
                            },
                        )
                    else:
                        # Cache exists but is stale - queue for update and return stale content
                        if aid not in pending_aids:
                            pending_aids.add(aid)
                            await update_queue.put(aid)
                        
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
                                "X-Age-Days": str(age.days),
                            },
                        )
                else:
                    # File exists but no DB entry - treat as stale
                    if aid not in pending_aids:
                        pending_aids.add(aid)
                        await update_queue.put(aid)
                    
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
    if aid not in pending_aids:
        pending_aids.add(aid)
        await update_queue.put(aid)

    # No cache available
    raise HTTPException(
        status_code=status.HTTP_202_ACCEPTED,
        detail=f"AID {aid} queued for fetching. Check back in a few moments.",
    )


@app.get("/search/tags", dependencies=[Depends(authenticate)])
async def search_by_tags(tags: str, min_weight: int = 200) -> Dict[str, Any]:
    """
    Search for anime by tags.
    Example: /search/tags?tags=action,comedy&min_weight=300
    """
    tag_list = [t.strip().lower() for t in tags.split(",")]

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            placeholders = ",".join("?" * len(tag_list))
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
            results = await cursor.fetchall()

        return {
            "query": tag_list,
            "min_weight": min_weight,
            "results": [{"aid": aid, "tag_matches": count} for aid, count in results],
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Search error: {str(e)}",
        )
