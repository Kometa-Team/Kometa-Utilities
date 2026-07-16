"""IMDB Service - FastAPI caching service for IMDB public datasets."""

import asyncio
import hashlib
import json
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Optional, cast
from urllib.parse import unquote, urlsplit

import aiosqlite
import charts
import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from importer import ALLOWED_TABLES, SCHEMA_SQL, TABLE_TO_STEM

# --- Config ---
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
DB_PATH = DATA_DIR / "imdb.db"
CHART_CACHE_PATH = DATA_DIR / "chart_cache.json"
ROOT_PATH = os.getenv("ROOT_PATH", "")
REFRESH_HOUR = int(os.getenv("REFRESH_HOUR", "3"))
MIN_VOTES_CHART = int(os.getenv("MIN_VOTES_CHART", "25000"))
PARENTAL_GUIDE_TTL_DAYS = int(os.getenv("PARENTAL_GUIDE_TTL_DAYS", "90"))
PARENTAL_BROWSER_ENABLED = os.getenv("PARENTAL_BROWSER_ENABLED", "true").lower() == "true"
PARENTAL_BROWSER_TIMEOUT_SECONDS = int(os.getenv("PARENTAL_BROWSER_TIMEOUT_SECONDS", "30"))
PARENTAL_BROWSER_NAV_TIMEOUT_SECONDS = int(os.getenv("PARENTAL_BROWSER_NAV_TIMEOUT_SECONDS", "120"))
PARENTAL_BROWSER_SELECTOR_TIMEOUT_SECONDS = int(
    os.getenv("PARENTAL_BROWSER_SELECTOR_TIMEOUT_SECONDS", "60")
)
PARENTAL_BROWSER_RETRY_COUNT = int(os.getenv("PARENTAL_BROWSER_RETRY_COUNT", "2"))
PARENTAL_BROWSER_CONCURRENCY = int(os.getenv("PARENTAL_BROWSER_CONCURRENCY", "2"))
PARENTAL_BROWSER_USER_DATA_DIR = Path(
    os.getenv("PARENTAL_BROWSER_USER_DATA_DIR", str(DATA_DIR / "playwright"))
)
PARENTAL_DECODO_BROWSER_ENABLED = (
    os.getenv("PARENTAL_DECODO_BROWSER_ENABLED", "false").lower() == "true"
)
PARENTAL_DECODO_BROWSER_HOST = os.getenv("PARENTAL_DECODO_BROWSER_HOST", "gate.decodo.com")
PARENTAL_DECODO_BROWSER_PORT = int(os.getenv("PARENTAL_DECODO_BROWSER_PORT", "7000"))
PARENTAL_DECODO_BROWSER_USERNAME = os.getenv("PARENTAL_DECODO_BROWSER_USERNAME", "").strip()
PARENTAL_DECODO_BROWSER_PASSWORD = os.getenv("PARENTAL_DECODO_BROWSER_PASSWORD", "").strip()
PARENTAL_DECODO_BROWSER_COUNTRY = os.getenv("PARENTAL_DECODO_BROWSER_COUNTRY", "").strip().lower()
PARENTAL_DECODO_BROWSER_SESSION_DURATION_MINUTES = int(
    os.getenv("PARENTAL_DECODO_BROWSER_SESSION_DURATION_MINUTES", "60")
)
PARENTAL_PROXY_ENABLED = os.getenv("PARENTAL_PROXY_ENABLED", "false").lower() == "true"
PARENTAL_PROXY_URLS = [
    p.strip() for p in os.getenv("PARENTAL_PROXY_URLS", "").split(",") if p.strip()
]
PARENTAL_PROXY_RETRY_COUNT = int(os.getenv("PARENTAL_PROXY_RETRY_COUNT", "2"))
PARENTAL_PROXY_BAN_TTL_MINUTES = int(os.getenv("PARENTAL_PROXY_BAN_TTL_MINUTES", "30"))
IMDB_WEB_BASE_URL = "https://www.imdb.com"
PARENTAL_GUIDE_LOGGING_ENABLED = (
    os.getenv("PARENTAL_GUIDE_LOGGING_ENABLED", "true").lower() == "true"
)
PARENTAL_BROWSER_SCREENSHOT_DIR = Path(
    os.getenv("PARENTAL_BROWSER_SCREENSHOT_DIR", str(DATA_DIR / "parental-failures"))
)

# --- Global state ---
last_refresh: Optional[str] = None  # ISO 8601 UTC string
refresh_worker_task: Optional[asyncio.Task] = None
current_phase: str = "idle"  # idle | downloading | importing | building_charts
download_progress: Dict[str, str] = {}  # dataset stem → pending|downloading|done
import_progress: Dict[str, Any] = {}  # table → {status, rows}
chart_progress: Optional[Dict[str, Any]] = None  # {current, completed, total}
last_activity: Optional[str] = None  # ISO timestamp of last phase change
_refresh_lock: asyncio.Lock = asyncio.Lock()  # guards manual single-table refreshes
proxy_health: Dict[str, datetime] = {}  # proxy URL -> cooldown-until UTC
parental_browser_contexts: Dict[str, Any] = {}
parental_browser_manager: Any = None
parental_browser_lock: Optional[asyncio.Lock] = None
parental_browser_semaphore: Optional[asyncio.Semaphore] = None
parental_decodo_browser_session_id: Optional[str] = None
PARENTAL_PAGE_READY_SELECTORS = (
    'section[data-testid="advisory-nudity"]',
    'section[data-testid^="advisory-"]',
    "li.ipc-metadata-list-item--link",
)


def _set_phase(phase: str) -> None:
    """Update current_phase and last_activity timestamp atomically."""
    global current_phase, last_activity
    current_phase = phase
    last_activity = datetime.now(timezone.utc).isoformat()


def _proxy_log_label(proxy_url: Optional[str]) -> str:
    """Return a safe proxy label for logs without exposing credentials."""
    if PARENTAL_DECODO_BROWSER_ENABLED:
        return "decodo-browser"
    if not proxy_url:
        return "direct"
    try:
        parsed = urlsplit(proxy_url)
        host = parsed.hostname or "unknown"
        port = f":{parsed.port}" if parsed.port is not None else ""
        return f"{parsed.scheme}://{host}{port}"
    except Exception:
        return "invalid-proxy"


async def _safe_page_content(page: Any, max_retries: int = 3) -> str:
    """Call page.content() safely, retrying if the page is still navigating.

    Playwright raises an error when page.content() is called while the page is
    actively navigating. This helper waits for networkidle between retries.
    """
    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    except ImportError:
        PlaywrightTimeoutError = Exception  # type: ignore[misc,assignment]

    last_error: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            return cast(str, await page.content())
        except Exception as e:
            last_error = e
            err_msg = str(e).lower()
            if "navigating" in err_msg or "changing the content" in err_msg:
                _parental_log(
                    "browser_content_navigating",
                    attempt=attempt + 1,
                    max_retries=max_retries,
                    error=str(e),
                )
                try:
                    await page.wait_for_load_state("networkidle", timeout=5000)
                except PlaywrightTimeoutError:
                    await page.wait_for_timeout(500)
                continue
            raise
    raise last_error  # type: ignore[misc]


def _parental_log(event: str, imdb_id: Optional[str] = None, **fields: Any) -> None:
    """Emit structured logs for the parental-guide fetch flow."""
    if not PARENTAL_GUIDE_LOGGING_ENABLED:
        return

    payload = {"event": event, "imdb_id": imdb_id, **fields}
    detail = " ".join(f"{key}={value}" for key, value in payload.items() if value is not None)
    print(f"[imdb-parental] {detail}")


async def _save_parental_failure_screenshot(
    page: Any, imdb_id: str, attempt: int, reason: str
) -> Optional[str]:
    """Capture a screenshot of the browser state for parental-guide failures."""
    try:
        PARENTAL_BROWSER_SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        safe_reason = "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in reason)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = PARENTAL_BROWSER_SCREENSHOT_DIR / (
            f"{imdb_id}-attempt{attempt}-{safe_reason}-{timestamp}.png"
        )
        await page.screenshot(path=str(path), full_page=True)
        _parental_log("browser_failure_screenshot_saved", imdb_id, attempt=attempt, path=str(path))
        return str(path)
    except Exception as e:
        _parental_log(
            "browser_failure_screenshot_error",
            imdb_id,
            attempt=attempt,
            error=type(e).__name__,
        )
        return None


async def _save_parental_failure_artifacts(
    page: Any, imdb_id: str, attempt: int, reason: str
) -> None:
    """Capture screenshot, HTML, and basic page metadata for browser failures."""
    screenshot_path = await _save_parental_failure_screenshot(page, imdb_id, attempt, reason)
    try:
        PARENTAL_BROWSER_SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        safe_reason = "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in reason)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        stem = f"{imdb_id}-attempt{attempt}-{safe_reason}-{timestamp}"
        html_path = PARENTAL_BROWSER_SCREENSHOT_DIR / f"{stem}.html"
        meta_path = PARENTAL_BROWSER_SCREENSHOT_DIR / f"{stem}.json"

        html_text = await _safe_page_content(page)
        html_path.write_text(html_text, encoding="utf-8")

        metadata = {
            "imdb_id": imdb_id,
            "attempt": attempt,
            "reason": reason,
            "timestamp": timestamp,
            "url": page.url,
            "title": await page.title(),
            "content_length": len(html_text),
            "has_waf_challenge": _html_has_waf_challenge(html_text),
            "has_no_parental_guide_notice": _html_has_no_parental_guide_notice(html_text),
            "has_parental_markers": _html_has_parental_markers(html_text),
            "screenshot_path": screenshot_path,
        }
        meta_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        _parental_log(
            "browser_failure_artifacts_saved",
            imdb_id,
            attempt=attempt,
            html_path=str(html_path),
            meta_path=str(meta_path),
            content_length=len(html_text),
            url=page.url,
            title=metadata["title"],
        )
    except Exception as e:
        _parental_log(
            "browser_failure_artifacts_error",
            imdb_id,
            attempt=attempt,
            error=type(e).__name__,
        )


def _proxy_candidates(exclude: Optional[set[str]] = None) -> list[str]:
    """Return currently healthy configured parental proxies."""
    if not PARENTAL_PROXY_ENABLED:
        return []
    now = datetime.now(timezone.utc)
    excluded = exclude or set()
    healthy = []
    for proxy in PARENTAL_PROXY_URLS:
        cooldown_until = proxy_health.get(proxy)
        if proxy in excluded:
            continue
        if cooldown_until is not None and cooldown_until > now:
            continue
        healthy.append(proxy)
    return healthy


def _choose_parental_proxy(exclude: Optional[set[str]] = None) -> Optional[str]:
    """Choose a healthy proxy for a parental fetch attempt."""
    candidates = _proxy_candidates(exclude)
    if not candidates:
        return None
    return secrets.choice(candidates)


def _mark_proxy_failed(proxy_url: str) -> None:
    """Temporarily cool down a proxy after a failed fetch."""
    proxy_health[proxy_url] = datetime.now(timezone.utc) + timedelta(
        minutes=PARENTAL_PROXY_BAN_TTL_MINUTES
    )


def _get_parental_browser_lock() -> asyncio.Lock:
    """Lazily create a lock for browser-context lifecycle operations."""
    global parental_browser_lock
    if parental_browser_lock is None:
        parental_browser_lock = asyncio.Lock()
    return parental_browser_lock


def _get_parental_browser_semaphore() -> asyncio.Semaphore:
    """Lazily create a concurrency limit for browser fallback fetches."""
    global parental_browser_semaphore
    if parental_browser_semaphore is None:
        parental_browser_semaphore = asyncio.Semaphore(PARENTAL_BROWSER_CONCURRENCY)
    return parental_browser_semaphore


def _parental_browser_context_key(proxy_url: Optional[str]) -> str:
    """Map a proxy configuration to a persistent browser-context key."""
    return _playwright_proxy_identity(proxy_url)[0]


def _parental_browser_user_data_dir(proxy_url: Optional[str]) -> Path:
    """Return a stable user-data dir for the direct or proxied browser context."""
    key = _parental_browser_context_key(proxy_url)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    label = "direct" if proxy_url is None else f"proxy-{digest}"
    return PARENTAL_BROWSER_USER_DATA_DIR / label


def _playwright_proxy_settings(proxy_url: Optional[str]) -> Optional[Dict[str, str]]:
    """Convert a proxy URL into Playwright's proxy settings shape."""
    return _playwright_proxy_identity(proxy_url)[1]


def _get_parental_decodo_browser_session_id() -> str:
    """Return a stable session identifier for the Decodo browser context."""
    global parental_decodo_browser_session_id
    if parental_decodo_browser_session_id is None:
        parental_decodo_browser_session_id = secrets.token_hex(8)
    return parental_decodo_browser_session_id


def _decodo_browser_proxy_settings() -> tuple[str, Dict[str, str]]:
    """Build sticky-session Playwright proxy settings for Decodo residential browsers."""
    if not PARENTAL_DECODO_BROWSER_HOST:
        raise HTTPException(status_code=502, detail="Decodo browser proxy host is not configured")
    if not PARENTAL_DECODO_BROWSER_USERNAME or not PARENTAL_DECODO_BROWSER_PASSWORD:
        raise HTTPException(
            status_code=502,
            detail="Decodo browser proxy credentials are not configured",
        )

    session_id = _get_parental_decodo_browser_session_id()
    username_parts = [PARENTAL_DECODO_BROWSER_USERNAME]
    if PARENTAL_DECODO_BROWSER_COUNTRY:
        username_parts.append(f"country-{PARENTAL_DECODO_BROWSER_COUNTRY}")
    username_parts.append(f"session-{session_id}")
    username_parts.append(f"sessionduration-{PARENTAL_DECODO_BROWSER_SESSION_DURATION_MINUTES}")

    context_key = (
        "decodo:"
        f"{PARENTAL_DECODO_BROWSER_HOST}:{PARENTAL_DECODO_BROWSER_PORT}:"
        f"{PARENTAL_DECODO_BROWSER_COUNTRY or 'any'}:"
        f"{PARENTAL_DECODO_BROWSER_SESSION_DURATION_MINUTES}:{session_id}"
    )
    return (
        context_key,
        {
            "server": f"http://{PARENTAL_DECODO_BROWSER_HOST}:{PARENTAL_DECODO_BROWSER_PORT}",
            "username": "-".join(username_parts),
            "password": PARENTAL_DECODO_BROWSER_PASSWORD,
        },
    )


def _playwright_proxy_identity(proxy_url: Optional[str]) -> tuple[str, Optional[Dict[str, str]]]:
    """Return a context key and Playwright proxy settings for the browser fallback."""
    if PARENTAL_DECODO_BROWSER_ENABLED:
        return _decodo_browser_proxy_settings()

    if not proxy_url:
        return "__direct__", None

    parsed = urlsplit(proxy_url)
    if not parsed.scheme or not parsed.hostname:
        raise HTTPException(status_code=502, detail=f"Invalid parental proxy URL: {proxy_url!r}")

    server = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port is not None:
        server += f":{parsed.port}"

    proxy_settings = {"server": server}
    if parsed.username is not None:
        proxy_settings["username"] = unquote(parsed.username)
    if parsed.password is not None:
        proxy_settings["password"] = unquote(parsed.password)
    return proxy_url, proxy_settings


async def _get_parental_browser_context(proxy_url: Optional[str]) -> Any:
    """Get or create a long-lived persistent browser context for parental fetches."""
    global parental_browser_manager

    context_key = _parental_browser_context_key(proxy_url)
    existing = parental_browser_contexts.get(context_key)
    if existing is not None:
        _parental_log(
            "browser_context_reused",
            proxy=_proxy_log_label(proxy_url),
            context_key=context_key,
        )
        return existing

    async with _get_parental_browser_lock():
        existing = parental_browser_contexts.get(context_key)
        if existing is not None:
            _parental_log(
                "browser_context_reused",
                proxy=_proxy_log_label(proxy_url),
                context_key=context_key,
            )
            return existing

        from playwright.async_api import async_playwright
        from playwright_stealth import Stealth

        if parental_browser_manager is None:
            parental_browser_manager = await async_playwright().start()

        user_data_dir = _parental_browser_user_data_dir(proxy_url)
        user_data_dir.mkdir(parents=True, exist_ok=True)

        launch_kwargs: Dict[str, Any] = {
            "user_data_dir": str(user_data_dir),
            "headless": True,
            "locale": "en-US",
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
        }
        proxy_settings = _playwright_proxy_settings(proxy_url)
        if proxy_settings:
            launch_kwargs["proxy"] = proxy_settings

        _parental_log(
            "browser_context_creating",
            proxy=_proxy_log_label(proxy_url),
            context_key=context_key,
            user_data_dir=str(user_data_dir),
        )
        context = await parental_browser_manager.chromium.launch_persistent_context(**launch_kwargs)
        context.set_default_navigation_timeout(PARENTAL_BROWSER_NAV_TIMEOUT_SECONDS * 1000)
        context.set_default_timeout(PARENTAL_BROWSER_SELECTOR_TIMEOUT_SECONDS * 1000)
        await Stealth().apply_stealth_async(context)
        parental_browser_contexts[context_key] = context
        _parental_log(
            "browser_context_ready",
            proxy=_proxy_log_label(proxy_url),
            context_key=context_key,
        )
        return context


async def _close_parental_browser_contexts() -> None:
    """Close any cached parental browser contexts and the shared Playwright manager."""
    global parental_browser_manager

    async with _get_parental_browser_lock():
        for context in parental_browser_contexts.values():
            try:
                await context.close()
            except Exception:
                pass  # nosec B110
        parental_browser_contexts.clear()

        if parental_browser_manager is not None:
            try:
                await parental_browser_manager.stop()
            except Exception:
                pass  # nosec B110
            parental_browser_manager = None


async def _ensure_db_schema() -> None:
    """Apply idempotent schema creation for an existing database file."""
    if not DB_PATH.exists():
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.executescript(SCHEMA_SQL)
        await db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application startup and shutdown: load DB state, rebuild charts, start scheduler."""
    global last_refresh, refresh_worker_task

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print("🔧 Initializing IMDB Service...")

    if DB_PATH.exists():
        try:
            await _ensure_db_schema()
        except Exception as e:
            print(f"⚠️  Could not apply schema updates: {e}")

        # Load last refresh time from DB
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    "SELECT value FROM import_meta WHERE key = 'last_refresh'"
                )
                row = await cursor.fetchone()
                if row:
                    last_refresh = row[0]
        except Exception as e:
            print(f"⚠️  Could not read last_refresh: {e}")

        # Rebuild chart cache from existing DB in the background so startup
        # completes immediately (a full rebuild scans millions of rows).
        refresh_worker_task = asyncio.create_task(_rebuild_charts_then_schedule())
        print("✅ IMDB Service ready")
        yield
    else:
        print("⚠️  No DB found — starting initial import in background")
        refresh_worker_task = asyncio.create_task(_initial_import_then_schedule())
        yield

    print("🛑 Shutting down...")
    if refresh_worker_task:
        refresh_worker_task.cancel()
        try:
            await refresh_worker_task
        except asyncio.CancelledError:
            pass
    await _close_parental_browser_contexts()


async def _run_import_pipeline() -> None:
    """Download datasets and import into shadow DB, then rebuild charts."""
    global last_refresh, download_progress, import_progress, chart_progress
    from importer import DATASET_FILES, STEM_TO_TABLE, download_datasets, run_direct_import

    print("🔄 Starting daily refresh...")

    # --- Download phase ---
    _set_phase("downloading")
    download_progress = {stem: "pending" for stem in DATASET_FILES}
    import_progress = {}
    chart_progress = None

    def _on_file_start(filename: str) -> None:
        for stem, fname in DATASET_FILES.items():
            if fname == filename:
                download_progress[stem] = "downloading"
                break

    def _on_file_done(filename: str) -> None:
        for stem, fname in DATASET_FILES.items():
            if fname == filename:
                download_progress[stem] = "done"
                break

    gz_paths, changed_stems = await download_datasets(DATA_DIR, _on_file_start, _on_file_done)
    if not changed_stems and DB_PATH.exists():
        _set_phase("idle")
        print("✅ Refresh skipped: no dataset changes detected")
        return

    # --- Import phase ---
    _set_phase("importing")
    import_progress = {
        STEM_TO_TABLE[stem]: {"status": "pending", "rows": 0}
        for stem in changed_stems
        if stem in STEM_TO_TABLE
    }

    def _on_table_start(table: str) -> None:
        import_progress[table] = {"status": "importing", "rows": 0}

    def _on_table_progress(table: str, count: int) -> None:
        import_progress[table] = {"status": "importing", "rows": count}

    def _on_table_done(table: str, count: int) -> None:
        import_progress[table] = {"status": "done", "rows": count}

    await asyncio.to_thread(
        run_direct_import,
        gz_paths,
        DB_PATH,
        changed_stems,
        None,
        _on_table_start,
        _on_table_done,
        _on_table_progress,
    )

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("SELECT value FROM import_meta WHERE key = 'last_refresh'")
            row = await cursor.fetchone()
            if row:
                last_refresh = row[0]
    except Exception as e:
        print(f"⚠️  Could not read last_refresh after import: {e}")

    # --- Chart rebuild phase ---
    _set_phase("building_charts")
    try:

        def _on_chart_progress(name: str, completed: int, total: int) -> None:
            global chart_progress
            chart_progress = {"current": name, "completed": completed, "total": total}

        await asyncio.to_thread(
            charts.rebuild_all_charts,
            DB_PATH,
            MIN_VOTES_CHART,
            _on_chart_progress,
            CHART_CACHE_PATH,
        )
    finally:
        chart_progress = None

    _set_phase("idle")
    print("✅ Refresh complete")


async def _refresh_single_table(stem: str) -> None:
    """Download and re-import a single dataset, then rebuild charts."""
    global last_refresh, download_progress, import_progress, chart_progress
    from importer import DATASET_FILES, STEM_TO_TABLE, download_datasets, run_direct_import

    async with _refresh_lock:
        table = STEM_TO_TABLE[stem]
        print(f"🔄 Manual refresh for {stem}...")

        _set_phase("downloading")
        download_progress = {stem: "pending"}
        import_progress = {}
        chart_progress = None

        def _on_file_start(filename: str) -> None:
            for s, fname in DATASET_FILES.items():
                if fname == filename:
                    download_progress[s] = "downloading"
                    break

        def _on_file_done(filename: str) -> None:
            for s, fname in DATASET_FILES.items():
                if fname == filename:
                    download_progress[s] = "done"
                    break

        gz_paths, _changed = await download_datasets(
            DATA_DIR, _on_file_start, _on_file_done, stems=[stem]
        )

        _set_phase("importing")
        import_progress = {table: {"status": "pending", "rows": 0}}

        def _on_table_start(t: str) -> None:
            import_progress[t] = {"status": "importing", "rows": 0}

        def _on_table_progress(t: str, count: int) -> None:
            import_progress[t] = {"status": "importing", "rows": count}

        def _on_table_done(t: str, count: int) -> None:
            import_progress[t] = {"status": "done", "rows": count}

        await asyncio.to_thread(
            run_direct_import,
            gz_paths,
            DB_PATH,
            [stem],
            None,
            _on_table_start,
            _on_table_done,
            _on_table_progress,
        )

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    "SELECT value FROM import_meta WHERE key = 'last_refresh'"
                )
                row = await cursor.fetchone()
                if row:
                    last_refresh = row[0]
        except Exception as e:
            print(f"⚠️  Could not read last_refresh after import: {e}")

        _set_phase("building_charts")
        try:

            def _on_chart_progress(name: str, completed: int, total: int) -> None:
                global chart_progress
                chart_progress = {"current": name, "completed": completed, "total": total}

            await asyncio.to_thread(
                charts.rebuild_all_charts,
                DB_PATH,
                MIN_VOTES_CHART,
                _on_chart_progress,
                CHART_CACHE_PATH,
            )
        finally:
            chart_progress = None

        _set_phase("idle")
        print(f"✅ Manual refresh for {stem} complete")


async def _initial_import_then_schedule() -> None:
    """Run initial import immediately, then hand off to scheduler."""
    try:
        await _run_import_pipeline()
    except Exception as e:
        print(f"❌ Initial import failed: {e}")
    await _refresh_scheduler()


async def _rebuild_charts_then_schedule() -> None:
    """Load or rebuild the chart cache from the existing DB, then start the scheduler."""
    global chart_progress

    # If a saved cache exists and is newer than the DB file, load it and skip the
    # expensive full scan. Charts are rebuilt after imports when the DB changes.
    if charts._cache_is_fresh(CHART_CACHE_PATH, DB_PATH) and charts.load_chart_cache(
        CHART_CACHE_PATH
    ):
        print("✅ Chart cache loaded from disk (fresh)")
        await _refresh_scheduler()
        return

    _set_phase("building_charts")
    try:

        def _on_chart_progress(name: str, completed: int, total: int) -> None:
            global chart_progress
            chart_progress = {"current": name, "completed": completed, "total": total}

        await asyncio.to_thread(
            charts.rebuild_all_charts,
            DB_PATH,
            MIN_VOTES_CHART,
            _on_chart_progress,
            CHART_CACHE_PATH,
        )
    except Exception as e:
        print(f"⚠️  Chart rebuild failed: {e}")
    finally:
        chart_progress = None
    await _refresh_scheduler()


async def _refresh_scheduler() -> None:
    """Sleep until REFRESH_HOUR UTC daily, then run the import pipeline."""
    while True:
        now = datetime.now(timezone.utc)
        target = now.replace(hour=REFRESH_HOUR, minute=0, second=0, microsecond=0)
        if target <= now:
            target = target + timedelta(days=1)
        sleep_secs = (target - now).total_seconds()
        print(f"💤 Next refresh in {sleep_secs / 3600:.1f}h (at {target.isoformat()})")
        await asyncio.sleep(sleep_secs)
        try:
            await _run_import_pipeline()
        except Exception as e:
            print(f"❌ Scheduled refresh failed: {e}")


app = FastAPI(
    title="IMDB Service",
    lifespan=lifespan,
    root_path=ROOT_PATH,
)


def _db_is_ready() -> bool:
    return DB_PATH.exists()


SORT_COLUMN_MAP: Dict[str, str] = {
    "rating": "tr.averageRating",
    "votes": "tr.numVotes",
    "year": "tb.startYear",
    "title": "tb.primaryTitle",
}

EXTRACT_FIELD_SELECTS: Dict[str, str] = {
    "ratings": "tb.tconst, tb.primaryTitle, tb.titleType, tr.averageRating, tr.numVotes",
    "genres": "tb.tconst, tb.primaryTitle, tb.titleType, tb.genres",
}

PARENTAL_TYPE_MAP: Dict[str, str] = {
    "Sex & Nudity": "Nudity",
    "Violence & Gore": "Violence",
    "Profanity": "Profanity",
    "Alcohol, Drugs & Smoking": "Alcohol",
    "Frightening & Intense Scenes": "Frightening",
}

PARENTAL_DB_COLUMNS: Dict[str, str] = {
    "Nudity": "nudity",
    "Violence": "violence",
    "Profanity": "profanity",
    "Alcohol": "alcohol",
    "Frightening": "frightening",
}


def _parse_sort(sort_by: str) -> tuple[str, str]:
    """Parse 'rating.desc' → ('tr.averageRating', 'DESC'). Raises ValueError on invalid input."""
    parts = sort_by.rsplit(".", 1)
    col_key = parts[0]
    direction = parts[1].upper() if len(parts) == 2 else "DESC"
    if col_key not in SORT_COLUMN_MAP:
        raise ValueError(f"Invalid sort column: {col_key!r}")
    if direction not in ("ASC", "DESC"):
        raise ValueError(f"Invalid sort direction: {direction!r}")
    return SORT_COLUMN_MAP[col_key], direction


def _normalize_extract_row(field: str, row: aiosqlite.Row) -> Dict[str, Any]:
    """Convert a raw extraction row into the endpoint response shape."""
    result: Dict[str, Any] = {
        "tconst": row["tconst"],
        "primaryTitle": row["primaryTitle"],
        "titleType": row["titleType"],
    }

    if field == "ratings":
        result["averageRating"] = row["averageRating"]
        result["numVotes"] = row["numVotes"]
    else:
        result["genres"] = row["genres"].split(",") if row["genres"] else []

    return result


class _ParentalGuideParser(HTMLParser):
    """Extract IMDb parental guide severity labels from the page HTML."""

    def __init__(self) -> None:
        super().__init__()
        self.results: Dict[str, str] = {}
        self._in_target_li = False
        self._li_depth = 0
        self._in_anchor = False
        self._anchor_text: list[str] = []
        self._other_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attrs_dict = dict(attrs)
        class_name = attrs_dict.get("class") or ""
        if tag == "li" and "ipc-metadata-list-item--link" in class_name and not self._in_target_li:
            self._in_target_li = True
            self._li_depth = 1
            self._anchor_text = []
            self._other_text = []
            return
        if self._in_target_li:
            if tag == "li":
                self._li_depth += 1
            elif tag == "a":
                self._in_anchor = True

    def handle_endtag(self, tag: str) -> None:
        if not self._in_target_li:
            return
        if tag == "a":
            self._in_anchor = False
        elif tag == "li":
            self._li_depth -= 1
            if self._li_depth == 0:
                category = "".join(self._anchor_text).strip().rstrip(":")
                severity = next(
                    (text for text in self._other_text if text and text != category), None
                )
                if category in PARENTAL_TYPE_MAP and severity:
                    self.results[PARENTAL_TYPE_MAP[category]] = severity
                self._in_target_li = False

    def handle_data(self, data: str) -> None:
        if not self._in_target_li:
            return
        text = unescape(data).strip()
        if not text:
            return
        if self._in_anchor:
            self._anchor_text.append(text)
        else:
            self._other_text.append(text)


def _normalize_parental_payload(values: Dict[str, Optional[str]]) -> Dict[str, str]:
    """Fill missing parental categories with 'None' to match Kometa cache reads."""
    return {category: values.get(category) or "None" for category in PARENTAL_TYPE_MAP.values()}


def _extract_parental_guide_regex(html_text: str) -> Dict[str, str]:
    """Fallback regex extractor for IMDb parental-guide severity labels."""
    decoded = unescape(html_text)
    results: Dict[str, str] = {}
    for category, key in PARENTAL_TYPE_MAP.items():
        # IMDb renders each rating item as <li data-testid="rating-item"> with the
        # category in an <a> and the severity in a nested <div class="ipc-html-content-inner-div">.
        regex = re.compile(
            re.escape(category) + r".*?<div[^>]*ipc-html-content-inner-div[^>]*>([^<]+)</div>",
            re.IGNORECASE | re.DOTALL,
        )
        match = regex.search(decoded)
        if match:
            results[key] = match.group(1).strip()
    return results


def _parse_parental_guide_html(html_text: str) -> Dict[str, str]:
    """Parse IMDb parental-guide HTML into Kometa-compatible category labels."""
    # If the browser path extracted ratings via JS, it injected a JSON comment.
    # Check this first because it is the most reliable source.
    js_match = re.search(r"<!-- kometa-parental-js:({.*?}) -->", html_text)
    if js_match:
        try:
            js_results = json.loads(js_match.group(1))
            if js_results and isinstance(js_results, dict):
                _parental_log("parse_js_injection_used", categories=",".join(js_results.keys()))
                return _normalize_parental_payload(js_results)
        except json.JSONDecodeError:
            pass

    if _html_has_no_parental_guide_notice(html_text):
        raise HTTPException(status_code=404, detail="No parental guide found")

    parser = _ParentalGuideParser()
    parser.feed(html_text)
    parsed = _normalize_parental_payload(parser.results)
    if not all(value == "None" for value in parsed.values()):
        return parsed

    # The structured parser missed the labels; try a regex fallback.
    regex_results = _extract_parental_guide_regex(html_text)
    parsed = _normalize_parental_payload(regex_results)
    if all(value == "None" for value in parsed.values()):
        raise HTTPException(status_code=404, detail="No parental guide found")
    return parsed


def _html_has_parental_markers(html_text: str) -> bool:
    """Return True when the HTML appears to contain IMDb parental guide categories."""
    decoded_html = unescape(html_text)
    return any(category in decoded_html for category in PARENTAL_TYPE_MAP)


def _html_has_waf_challenge(html_text: str) -> bool:
    """Return True when IMDb served an AWS WAF challenge/interstitial page."""
    lowered = html_text.lower()
    waf_markers = (
        "awswafintegration",
        "challenge.js",
        "token.awswaf.com",
        "not a robot",
        "javascript is disabled",
    )
    return any(marker in lowered for marker in waf_markers)


def _html_has_no_parental_guide_notice(html_text: str) -> bool:
    """Return True when IMDb explicitly says the title has no parents guide yet."""
    lowered = unescape(html_text).lower()
    return (
        "we don't have a parents guide for this title yet" in lowered
        or "we do not have a parents guide for this title yet" in lowered
        or "be the first to contribute" in lowered
        and "parental_guide" in lowered
    )


BLOCK_PAGE_MARKERS = (
    "403 forbidden",
    "403 - forbidden",
    "access denied",
    "access to this page has been denied",
    "you have been blocked",
    "request blocked",
    "error code 1020",
    "verify you are human",
    "please verify you are a human",
    "robot or human",
    "recaptcha",
    "g-recaptcha",
    "cf-challenge",
    "checking your browser",
    "please wait while your request is being verified",
)


def _html_looks_blocked(html_text: str) -> bool:
    """Return True when the HTML appears to be a block/WAF/human-verification page."""
    lowered = unescape(html_text).lower()
    return any(marker in lowered for marker in BLOCK_PAGE_MARKERS)


async def _wait_for_parental_page_ready(page: Any) -> None:
    """Wait until the parental-guide page exposes a stable advisory element."""
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    advisory_selector = ", ".join(PARENTAL_PAGE_READY_SELECTORS)
    deadline = asyncio.get_running_loop().time() + PARENTAL_BROWSER_NAV_TIMEOUT_SECONDS
    not_ready_count = 0

    while asyncio.get_running_loop().time() < deadline:
        html_text = await _safe_page_content(page)
        if _html_has_parental_markers(html_text):
            _parental_log("browser_selector_ready", selector=advisory_selector)
            return
        if _html_has_no_parental_guide_notice(html_text):
            _parental_log("browser_no_guide_notice_detected")
            raise HTTPException(status_code=404, detail="No parental guide found")
        if _html_looks_blocked(html_text):
            _parental_log("browser_page_looks_blocked")
            raise HTTPException(
                status_code=403,
                detail="Playwright parental guide fetch received a blocked/access-denied page",
            )

        remaining_ms = max(1, int((deadline - asyncio.get_running_loop().time()) * 1000))
        slice_timeout_ms = min(2000, remaining_ms)
        if _html_has_waf_challenge(html_text):
            _parental_log("browser_waf_still_present", timeout_ms=slice_timeout_ms)
            await page.wait_for_timeout(slice_timeout_ms)
            continue

        try:
            await page.wait_for_function(
                """
                ({ advisorySelector }) => {
                    const advisoryFound = Boolean(document.querySelector(advisorySelector));
                    const text = (document.body?.innerText || "").toLowerCase();
                    const noGuideFound =
                        text.includes("we don't have a parents guide for this title yet") ||
                        text.includes("we do not have a parents guide for this title yet") ||
                        (text.includes("be the first to contribute") && text.includes("parents guide"));
                    return advisoryFound || noGuideFound;
                }
                """,
                {"advisorySelector": advisory_selector},
                timeout=slice_timeout_ms,
            )
        except Exception:
            not_ready_count += 1
            if not_ready_count % 5 == 1:
                _parental_log(
                    "browser_page_not_ready",
                    timeout_ms=slice_timeout_ms,
                    not_ready_count=not_ready_count,
                )

        try:
            await page.wait_for_load_state("domcontentloaded", timeout=min(1000, slice_timeout_ms))
        except PlaywrightTimeoutError:
            if not_ready_count % 5 == 1:
                _parental_log(
                    "browser_load_state_wait_timed_out",
                    timeout_ms=min(1000, slice_timeout_ms),
                )
        await page.wait_for_timeout(250)

    html_text = await _safe_page_content(page)
    if _html_has_parental_markers(html_text):
        _parental_log("browser_selector_ready", selector=advisory_selector)
        return
    if _html_has_no_parental_guide_notice(html_text):
        _parental_log("browser_no_guide_notice_detected")
        raise HTTPException(status_code=404, detail="No parental guide found")
    if _html_looks_blocked(html_text):
        raise HTTPException(
            status_code=403,
            detail="Playwright parental guide fetch received a blocked/access-denied page",
        )
    if _html_has_waf_challenge(html_text):
        raise HTTPException(
            status_code=504,
            detail="Playwright parental guide fetch timed out before the page cleared the WAF challenge",
        )
    raise HTTPException(
        status_code=504,
        detail="Playwright parental guide fetch timed out before advisory content appeared",
    )


async def _fetch_parental_guide_html_via_http(imdb_id: str, proxy_url: Optional[str] = None) -> str:
    """Fetch the IMDb parental guide page for a title via direct HTTP."""
    url = f"{IMDB_WEB_BASE_URL}/title/{imdb_id}/parentalguide"
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; Kometa-Utilities/IMDb-Service)",
        "Accept-Language": "en-US,en;q=0.9",
    }
    started = time.monotonic()
    _parental_log("http_fetch_start", imdb_id, proxy=_proxy_log_label(proxy_url), url=url)
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=30.0,
            headers=headers,
            proxy=proxy_url,
        ) as client:
            response = await client.get(url)
            _parental_log(
                "http_fetch_response",
                imdb_id,
                proxy=_proxy_log_label(proxy_url),
                status_code=response.status_code,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
            if response.status_code == 202:
                raise HTTPException(
                    status_code=502,
                    detail="IMDb parental guide request returned 202 Accepted without usable content",
                )
            response.raise_for_status()
            return response.text
    except httpx.HTTPStatusError as e:
        _parental_log(
            "http_fetch_error_status",
            imdb_id,
            proxy=_proxy_log_label(proxy_url),
            status_code=e.response.status_code,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
        if e.response.status_code == 404:
            status_code = 404
        elif e.response.status_code == 403:
            status_code = 403
        else:
            status_code = 502
        raise HTTPException(
            status_code=status_code,
            detail=f"IMDb parental guide request failed with status {e.response.status_code}",
        )
    except httpx.HTTPError as e:
        _parental_log(
            "http_fetch_error",
            imdb_id,
            proxy=_proxy_log_label(proxy_url),
            error=type(e).__name__,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
        raise HTTPException(status_code=502, detail=f"IMDb parental guide request failed: {e}")


async def _fetch_parental_guide_html_via_browser(
    imdb_id: str, proxy_url: Optional[str] = None
) -> str:
    """Fetch the IMDb parental guide page using a headless browser fallback."""
    if not PARENTAL_BROWSER_ENABLED:
        raise HTTPException(status_code=502, detail="Browser fallback is disabled")

    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    except ImportError as e:
        raise HTTPException(status_code=502, detail=f"Playwright is not installed: {e}")

    url = f"{IMDB_WEB_BASE_URL}/title/{imdb_id}/parentalguide"
    timeout_ms = PARENTAL_BROWSER_NAV_TIMEOUT_SECONDS * 1000
    browser_retries = max(1, PARENTAL_BROWSER_RETRY_COUNT)
    context = await _get_parental_browser_context(proxy_url)
    try:
        async with _get_parental_browser_semaphore():
            last_error: Optional[HTTPException] = None
            for attempt in range(browser_retries):
                started = time.monotonic()
                page = await context.new_page()
                try:
                    _parental_log(
                        "browser_fetch_start",
                        imdb_id,
                        proxy=_proxy_log_label(proxy_url),
                        attempt=attempt + 1,
                        retries=browser_retries,
                        url=url,
                    )
                    response = await page.goto(
                        url, wait_until="domcontentloaded", timeout=timeout_ms
                    )
                    await page.wait_for_timeout(2000)
                    if response and response.status == 404:
                        raise HTTPException(status_code=404, detail=f"Title {imdb_id!r} not found")
                    if response and response.status == 403:
                        raise HTTPException(
                            status_code=403,
                            detail="Playwright parental guide fetch received HTTP 403",
                        )

                    # Early check for a block page before the full ready-wait loop.
                    early_html = await _safe_page_content(page)
                    if _html_looks_blocked(early_html):
                        raise HTTPException(
                            status_code=403,
                            detail="Playwright parental guide fetch received a blocked/access-denied page",
                        )

                    await _wait_for_parental_page_ready(page)
                    # Give the page a moment to settle before reading content();
                    # otherwise Page.content can race with ongoing navigation.
                    await page.wait_for_timeout(1000)

                    # Extract ratings directly from the live DOM. This is more
                    # reliable than parsing the serialized HTML after JS rendering.
                    js_results = await page.evaluate("""
                        () => {
                            const labels = [
                                ["Sex & Nudity", "Nudity"],
                                ["Violence & Gore", "Violence"],
                                ["Profanity", "Profanity"],
                                ["Alcohol, Drugs & Smoking", "Alcohol"],
                                ["Frightening & Intense Scenes", "Frightening"]
                            ];
                            const results = {};
                            for (const [text, key] of labels) {
                                const items = Array.from(document.querySelectorAll('li[data-testid="rating-item"]'));
                                for (const item of items) {
                                    if (item.innerText.includes(text)) {
                                        const m = item.innerText.match(/(None|Mild|Moderate|Severe)/i);
                                        if (m) {
                                            results[key] = m[1].charAt(0).toUpperCase() + m[1].slice(1).toLowerCase();
                                        }
                                    }
                                }
                            }
                            return results;
                        }
                        """)
                    html_text = await _safe_page_content(page)
                    if js_results:
                        _parental_log(
                            "browser_js_extraction",
                            imdb_id,
                            results_count=len(js_results),
                            categories=",".join(js_results.keys()),
                        )
                        html_text += f"\n<!-- kometa-parental-js:{json.dumps(js_results)} -->\n"

                    if _html_has_parental_markers(html_text):
                        _parental_log(
                            "browser_fetch_success",
                            imdb_id,
                            proxy=_proxy_log_label(proxy_url),
                            attempt=attempt + 1,
                            elapsed_ms=int((time.monotonic() - started) * 1000),
                        )
                        return html_text
                    if _html_has_waf_challenge(html_text):
                        _parental_log(
                            "browser_fetch_waf_challenge",
                            imdb_id,
                            proxy=_proxy_log_label(proxy_url),
                            attempt=attempt + 1,
                            elapsed_ms=int((time.monotonic() - started) * 1000),
                        )
                        raise HTTPException(
                            status_code=504,
                            detail="Playwright parental guide fetch timed out before the page cleared the WAF challenge",
                        )
                    raise HTTPException(
                        status_code=502,
                        detail="Playwright parental guide fetch completed without IMDb advisory markers",
                    )
                except PlaywrightTimeoutError as e:
                    await _save_parental_failure_artifacts(
                        page, imdb_id, attempt + 1, "playwright-timeout"
                    )
                    _parental_log(
                        "browser_fetch_timeout",
                        imdb_id,
                        proxy=_proxy_log_label(proxy_url),
                        attempt=attempt + 1,
                        error=type(e).__name__,
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                    )
                    last_error = HTTPException(
                        status_code=504, detail=f"Playwright parental guide fetch timed out: {e}"
                    )
                except HTTPException as e:
                    await _save_parental_failure_artifacts(
                        page, imdb_id, attempt + 1, f"http-{e.status_code}"
                    )
                    _parental_log(
                        "browser_fetch_http_error",
                        imdb_id,
                        proxy=_proxy_log_label(proxy_url),
                        attempt=attempt + 1,
                        status_code=e.status_code,
                        detail=e.detail,
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                    )
                    if e.status_code == 404:
                        raise
                    last_error = e
                finally:
                    await page.close()

                if attempt < browser_retries - 1:
                    await asyncio.sleep(min(2 * (attempt + 1), 5))

            if last_error:
                raise last_error
            raise HTTPException(
                status_code=502,
                detail="Playwright parental guide fetch failed without a usable response",
            )
    except PlaywrightTimeoutError as e:
        raise HTTPException(
            status_code=504, detail=f"Playwright parental guide fetch timed out: {e}"
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Playwright parental guide fetch failed: {e}")


async def _fetch_parental_guide_html(imdb_id: str) -> str:
    """Fetch the IMDb parental guide page for a title, falling back to a browser if needed."""
    attempted_proxies: set[str] = set()
    attempts = max(1, PARENTAL_PROXY_RETRY_COUNT if PARENTAL_PROXY_ENABLED else 1)
    last_error: Optional[HTTPException] = None

    healthy_proxies = _proxy_candidates()
    _parental_log(
        "fetch_start",
        imdb_id,
        proxy_enabled=PARENTAL_PROXY_ENABLED,
        proxy_count_total=len(PARENTAL_PROXY_URLS),
        proxy_count_healthy=len(healthy_proxies),
        attempts=attempts,
        decodo_browser=PARENTAL_DECODO_BROWSER_ENABLED,
    )

    for attempt_index in range(attempts):
        proxy_url = _choose_parental_proxy(attempted_proxies)
        if proxy_url:
            attempted_proxies.add(proxy_url)
        elif PARENTAL_PROXY_ENABLED and PARENTAL_PROXY_URLS:
            # All configured proxies are currently on cooldown; log details so the
            # operator can see why we fell back to direct.
            now = datetime.now(timezone.utc)
            cooldowns = {
                proxy: int(max(0, (proxy_health[proxy] - now).total_seconds()))
                for proxy in PARENTAL_PROXY_URLS
                if proxy in proxy_health and proxy_health[proxy] > now
            }
            _parental_log(
                "fetch_attempt_no_healthy_proxy",
                imdb_id,
                attempt=attempt_index + 1,
                cooldowns=cooldowns,
            )
        _parental_log(
            "fetch_attempt",
            imdb_id,
            attempt=attempt_index + 1,
            proxy=_proxy_log_label(proxy_url),
        )
        try:
            html_text = await _fetch_parental_guide_html_via_http(imdb_id, proxy_url)
            if _html_has_parental_markers(html_text):
                _parental_log(
                    "fetch_http_success",
                    imdb_id,
                    attempt=attempt_index + 1,
                    proxy=_proxy_log_label(proxy_url),
                )
                return html_text
            _parental_log(
                "fetch_http_missing_markers",
                imdb_id,
                attempt=attempt_index + 1,
                proxy=_proxy_log_label(proxy_url),
            )
        except HTTPException as e:
            if e.status_code == 404:
                raise
            last_error = e
            if proxy_url:
                _mark_proxy_failed(proxy_url)
            _parental_log(
                "fetch_http_failed",
                imdb_id,
                attempt=attempt_index + 1,
                proxy=_proxy_log_label(proxy_url),
                status_code=e.status_code,
                detail=e.detail,
            )
            # Direct HTTP was blocked and we have no proxy/residential-browser bypass
            # configured. The browser fallback uses the same egress IP, so it will
            # almost certainly be blocked too. Fail fast instead of spinning.
            if (
                e.status_code == 403
                and proxy_url is None
                and not PARENTAL_PROXY_ENABLED
                and not PARENTAL_DECODO_BROWSER_ENABLED
            ):
                _parental_log(
                    "fetch_direct_blocked_no_bypass",
                    imdb_id,
                    proxy_enabled=PARENTAL_PROXY_ENABLED,
                    decodo_browser=PARENTAL_DECODO_BROWSER_ENABLED,
                )
                raise HTTPException(
                    status_code=403,
                    detail="IMDb blocked the direct parental guide request and no proxy or Decodo browser is configured",
                )

        try:
            html_text = await _fetch_parental_guide_html_via_browser(imdb_id, proxy_url)
            _parental_log(
                "fetch_browser_success",
                imdb_id,
                attempt=attempt_index + 1,
                proxy=_proxy_log_label(proxy_url),
            )
            return html_text
        except HTTPException as e:
            if e.status_code == 404:
                raise
            last_error = e
            if proxy_url:
                _mark_proxy_failed(proxy_url)
            _parental_log(
                "fetch_browser_failed",
                imdb_id,
                attempt=attempt_index + 1,
                proxy=_proxy_log_label(proxy_url),
                status_code=e.status_code,
                detail=e.detail,
            )

    if last_error:
        _parental_log(
            "fetch_failed",
            imdb_id,
            status_code=last_error.status_code,
            detail=last_error.detail,
        )
        raise last_error
    _parental_log("fetch_failed_no_response", imdb_id)
    raise HTTPException(
        status_code=502, detail="IMDb parental guide fetch failed without a usable response"
    )


async def _query_parental_cache(
    imdb_id: str,
) -> tuple[Optional[Dict[str, str]], Optional[bool], Optional[str]]:
    """Read cached parental-guide data, expiry flag, and last-updated timestamp."""
    await _ensure_db_schema()

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM imdb_parental WHERE imdb_id = ?", (imdb_id,))
        row = await cursor.fetchone()
        if not row:
            return None, None, None
        updated_at = row["updated_at"]
        expired = True
        if updated_at:
            updated_dt = datetime.fromisoformat(updated_at)
            if updated_dt.tzinfo is None:
                updated_dt = updated_dt.replace(tzinfo=timezone.utc)
            expired = datetime.now(timezone.utc) - updated_dt > timedelta(
                days=PARENTAL_GUIDE_TTL_DAYS
            )
        return (
            _normalize_parental_payload(
                {
                    "Nudity": row["nudity"],
                    "Violence": row["violence"],
                    "Profanity": row["profanity"],
                    "Alcohol": row["alcohol"],
                    "Frightening": row["frightening"],
                }
            ),
            expired,
            updated_at,
        )


def _parental_cache_age_days(cached_at: Optional[str]) -> Optional[int]:
    """Return the number of whole days since the cache was last updated."""
    if not cached_at:
        return None
    try:
        updated_dt = datetime.fromisoformat(cached_at)
        if updated_dt.tzinfo is None:
            updated_dt = updated_dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - updated_dt).days
    except (ValueError, TypeError):
        return None


async def _update_parental_cache(imdb_id: str, parental: Dict[str, str]) -> str:
    """Upsert cached parental-guide data for a title and return the updated_at timestamp."""
    await _ensure_db_schema()

    updated_at = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO imdb_parental(imdb_id, nudity, violence, profanity, alcohol, frightening, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(imdb_id) DO UPDATE SET
                nudity = excluded.nudity,
                violence = excluded.violence,
                profanity = excluded.profanity,
                alcohol = excluded.alcohol,
                frightening = excluded.frightening,
                updated_at = excluded.updated_at
            """,
            (
                imdb_id,
                parental["Nudity"],
                parental["Violence"],
                parental["Profanity"],
                parental["Alcohol"],
                parental["Frightening"],
                updated_at,
            ),
        )
        await db.commit()
    return updated_at


async def _get_parental_cache_stats() -> Dict[str, Any]:
    """Return cached parental-guide item counts for the stats endpoint."""
    await _ensure_db_schema()

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT
                COUNT(*) AS items_cached,
                SUM(CASE WHEN nudity IS NOT NULL AND nudity != '' THEN 1 ELSE 0 END) AS nudity,
                SUM(CASE WHEN violence IS NOT NULL AND violence != '' THEN 1 ELSE 0 END) AS violence,
                SUM(CASE WHEN profanity IS NOT NULL AND profanity != '' THEN 1 ELSE 0 END) AS profanity,
                SUM(CASE WHEN alcohol IS NOT NULL AND alcohol != '' THEN 1 ELSE 0 END) AS alcohol,
                SUM(CASE WHEN frightening IS NOT NULL AND frightening != '' THEN 1 ELSE 0 END) AS frightening
            FROM imdb_parental
            """)
        row = await cursor.fetchone()

    if row is None:
        return {
            "items_cached": 0,
            "flag_counts": {column: 0 for column in PARENTAL_DB_COLUMNS.values()},
        }

    return {
        "items_cached": row[0] or 0,
        "flag_counts": {
            "nudity": row[1] or 0,
            "violence": row[2] or 0,
            "profanity": row[3] or 0,
            "alcohol": row[4] or 0,
            "frightening": row[5] or 0,
        },
    }


@app.get("/search")
async def search(
    type: Optional[str] = Query(None, alias="type"),  # noqa: B008
    type_not: Optional[str] = Query(None, alias="type.not"),  # noqa: B008
    genre: Optional[str] = Query(None, alias="genre"),  # noqa: B008
    genre_any: Optional[str] = Query(None, alias="genre.any"),  # noqa: B008
    genre_not: Optional[str] = Query(None, alias="genre.not"),  # noqa: B008
    rating_gte: Optional[float] = Query(None, alias="rating.gte"),  # noqa: B008
    rating_lte: Optional[float] = Query(None, alias="rating.lte"),  # noqa: B008
    votes_gte: Optional[int] = Query(None, alias="votes.gte"),  # noqa: B008
    votes_lte: Optional[int] = Query(None, alias="votes.lte"),  # noqa: B008
    runtime_gte: Optional[int] = Query(None, alias="runtime.gte"),  # noqa: B008
    runtime_lte: Optional[int] = Query(None, alias="runtime.lte"),  # noqa: B008
    release_after: Optional[str] = Query(None, alias="release.after"),  # noqa: B008
    release_before: Optional[str] = Query(None, alias="release.before"),  # noqa: B008
    title: Optional[str] = None,
    adult: bool = False,
    imdb_top: Optional[int] = None,
    imdb_bottom: Optional[int] = None,
    sort_by: str = "rating.desc",
    limit: int = Query(default=100, le=1000),  # noqa: B008
    language: Optional[str] = Query(None, alias="language"),  # noqa: B008
    language_any: Optional[str] = Query(None, alias="language.any"),  # noqa: B008
    language_not: Optional[str] = Query(None, alias="language.not"),  # noqa: B008
    language_primary: Optional[str] = Query(None, alias="language.primary"),  # noqa: B008
    country: Optional[str] = Query(None, alias="country"),  # noqa: B008
    country_any: Optional[str] = Query(None, alias="country.any"),  # noqa: B008
    country_not: Optional[str] = Query(None, alias="country.not"),  # noqa: B008
    country_origin: Optional[str] = Query(None, alias="country.origin"),  # noqa: B008
    cast: Optional[str] = Query(None, alias="cast"),  # noqa: B008
    cast_any: Optional[str] = Query(None, alias="cast.any"),  # noqa: B008
    cast_not: Optional[str] = Query(None, alias="cast.not"),  # noqa: B008
    series: Optional[str] = Query(None, alias="series"),  # noqa: B008
    series_not: Optional[str] = Query(None, alias="series.not"),  # noqa: B008
) -> Dict[str, Any]:
    """Return a filtered list of IMDb IDs matching the given criteria."""
    if imdb_top is not None and imdb_bottom is not None:
        raise HTTPException(
            status_code=400, detail="imdb_top and imdb_bottom are mutually exclusive"
        )

    if not _db_is_ready():
        raise HTTPException(status_code=503, detail="Service initializing")

    try:
        sort_col, sort_dir = _parse_sort(sort_by)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    conditions: list[str] = []
    params: list = []

    if not adult:
        conditions.append("tb.isAdult = 0")

    if type:
        types = [t.strip() for t in type.split(",")]
        placeholders = ",".join("?" * len(types))
        conditions.append(f"tb.titleType IN ({placeholders})")
        params.extend(types)
    if type_not:
        types = [t.strip() for t in type_not.split(",")]
        placeholders = ",".join("?" * len(types))
        conditions.append(f"tb.titleType NOT IN ({placeholders})")
        params.extend(types)

    if genre:
        for g in genre.split(","):
            g = g.strip()
            conditions.append(
                "(tb.genres LIKE ? OR tb.genres LIKE ? OR tb.genres LIKE ? OR tb.genres = ?)"
            )
            params.extend([f"{g},%", f"%,{g},%", f"%,{g}", g])
    if genre_any:
        gs = [g.strip() for g in genre_any.split(",")]
        sub = " OR ".join(
            "(tb.genres LIKE ? OR tb.genres LIKE ? OR tb.genres LIKE ? OR tb.genres = ?)"
            for _ in gs
        )
        conditions.append(f"({sub})")
        for g in gs:
            params.extend([f"{g},%", f"%,{g},%", f"%,{g}", g])
    if genre_not:
        for g in genre_not.split(","):
            g = g.strip()
            conditions.append(
                "(tb.genres NOT LIKE ? AND tb.genres NOT LIKE ? AND tb.genres NOT LIKE ? AND tb.genres != ?)"
            )
            params.extend([f"{g},%", f"%,{g},%", f"%,{g}", g])

    if rating_gte is not None:
        conditions.append("tr.averageRating >= ?")
        params.append(rating_gte)
    if rating_lte is not None:
        conditions.append("tr.averageRating <= ?")
        params.append(rating_lte)
    if votes_gte is not None:
        conditions.append("tr.numVotes >= ?")
        params.append(votes_gte)
    if votes_lte is not None:
        conditions.append("tr.numVotes <= ?")
        params.append(votes_lte)
    if runtime_gte is not None:
        conditions.append("tb.runtimeMinutes >= ?")
        params.append(runtime_gte)
    if runtime_lte is not None:
        conditions.append("tb.runtimeMinutes <= ?")
        params.append(runtime_lte)

    def _year_from(s: str) -> int:
        if s.lower() == "today":
            return date.today().year
        try:
            return int(s[:4])
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid year value: {s!r}")

    if release_after:
        conditions.append("tb.startYear > ?")
        params.append(_year_from(release_after))
    if release_before:
        conditions.append("tb.startYear < ?")
        params.append(_year_from(release_before))

    if title:
        conditions.append("tb.primaryTitle LIKE ?")
        params.append(f"%{title}%")

    if imdb_top is not None:
        top_chart = charts.chart_cache.get("top_movies", [])
        allowed = [item["tconst"] for item in top_chart if item["rank"] <= imdb_top]
        if not allowed:
            return {"results": [], "total": 0}
        placeholders = ",".join("?" * len(allowed))
        conditions.append(f"tb.tconst IN ({placeholders})")
        params.extend(allowed)
    elif imdb_bottom is not None:
        bottom_chart = charts.chart_cache.get("lowest_rated", [])
        allowed = [item["tconst"] for item in bottom_chart if item["rank"] <= imdb_bottom]
        if not allowed:
            return {"results": [], "total": 0}
        placeholders = ",".join("?" * len(allowed))
        conditions.append(f"tb.tconst IN ({placeholders})")
        params.extend(allowed)

    _add_join_filters(
        conditions,
        params,
        language,
        language_any,
        language_not,
        language_primary,
        country,
        country_any,
        country_not,
        country_origin,
        cast,
        cast_any,
        cast_not,
        series,
        series_not,
    )

    where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""

    sql = (
        f"SELECT DISTINCT tb.tconst "  # nosec B608
        f"FROM title_basics tb "
        f"LEFT JOIN title_ratings tr ON tb.tconst = tr.tconst "
        f"{where_clause} "
        f"ORDER BY {sort_col} {sort_dir} "
        f"LIMIT ?"
    )

    params.append(limit)

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(sql, params)
            rows = await cursor.fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Search error: {e}")

    results = [row[0] for row in rows]

    return {"results": results, "total": len(results)}


def _add_join_filters(
    conditions: list[str],
    params: list[Any],
    language: Optional[str],
    language_any: Optional[str],
    language_not: Optional[str],
    language_primary: Optional[str],
    country: Optional[str],
    country_any: Optional[str],
    country_not: Optional[str],
    country_origin: Optional[str],
    cast: Optional[str],
    cast_any: Optional[str],
    cast_not: Optional[str],
    series: Optional[str],
    series_not: Optional[str],
) -> None:
    """Append WHERE conditions for join-based filters using EXISTS subqueries."""

    def _exists_aka_language(negate: bool = False) -> str:
        if negate:
            return (
                "NOT EXISTS (SELECT 1 FROM title_akas ta "
                "WHERE ta.tconst = tb.tconst AND ta.language = ?)"
            )
        return (
            "EXISTS (SELECT 1 FROM title_akas ta "
            "WHERE ta.tconst = tb.tconst AND ta.language = ?)"
        )

    def _exists_aka_region(negate: bool = False) -> str:
        if negate:
            return (
                "NOT EXISTS (SELECT 1 FROM title_akas ta "
                "WHERE ta.tconst = tb.tconst AND ta.region = ?)"
            )
        return (
            "EXISTS (SELECT 1 FROM title_akas ta " "WHERE ta.tconst = tb.tconst AND ta.region = ?)"
        )

    def _exists_aka_original_language() -> str:
        return (
            "EXISTS (SELECT 1 FROM title_akas ta "
            "WHERE ta.tconst = tb.tconst AND ta.language = ? AND ta.isOriginalTitle = 1)"
        )

    def _exists_aka_original_region() -> str:
        return (
            "EXISTS (SELECT 1 FROM title_akas ta "
            "WHERE ta.tconst = tb.tconst AND ta.region = ? AND ta.isOriginalTitle = 1)"
        )

    if language:
        for lang in language.split(","):
            conditions.append(_exists_aka_language())
            params.append(lang.strip())
    if language_any:
        langs = [lang.strip() for lang in language_any.split(",")]
        sub = " OR ".join(_exists_aka_language() for _ in langs)
        conditions.append(f"({sub})")
        params.extend(langs)
    if language_not:
        for lang in language_not.split(","):
            conditions.append(_exists_aka_language(negate=True))
            params.append(lang.strip())
    if language_primary:
        for lang in language_primary.split(","):
            conditions.append(_exists_aka_original_language())
            params.append(lang.strip())

    if country:
        for c in country.split(","):
            conditions.append(_exists_aka_region())
            params.append(c.strip())
    if country_any:
        cs = [c.strip() for c in country_any.split(",")]
        sub = " OR ".join(_exists_aka_region() for _ in cs)
        conditions.append(f"({sub})")
        params.extend(cs)
    if country_not:
        for c in country_not.split(","):
            conditions.append(_exists_aka_region(negate=True))
            params.append(c.strip())
    if country_origin:
        for c in country_origin.split(","):
            conditions.append(_exists_aka_original_region())
            params.append(c.strip())

    def _exists_cast(negate: bool = False) -> str:
        op = "NOT EXISTS" if negate else "EXISTS"
        return f"{op} (SELECT 1 FROM title_principals tp WHERE tp.tconst = tb.tconst AND tp.nconst = ?)"  # nosec B608

    if cast:
        for nm in cast.split(","):
            conditions.append(_exists_cast())
            params.append(nm.strip())
    if cast_any:
        nms = [nm.strip() for nm in cast_any.split(",")]
        sub = " OR ".join(_exists_cast() for _ in nms)
        conditions.append(f"({sub})")
        params.extend(nms)
    if cast_not:
        for nm in cast_not.split(","):
            conditions.append(_exists_cast(negate=True))
            params.append(nm.strip())

    if series:
        for s in series.split(","):
            conditions.append(
                "EXISTS (SELECT 1 FROM title_episode te WHERE te.tconst = tb.tconst AND te.parentTconst = ?)"
            )
            params.append(s.strip())
    if series_not:
        for s in series_not.split(","):
            conditions.append(
                "NOT EXISTS (SELECT 1 FROM title_episode te WHERE te.tconst = tb.tconst AND te.parentTconst = ?)"
            )
            params.append(s.strip())


@app.get("/endpoints", response_class=HTMLResponse)
async def endpoints(request: Request) -> HTMLResponse:
    """Return HTML landing page with links to available endpoints."""
    base = f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"
    if ROOT_PATH:
        base += ROOT_PATH
    return HTMLResponse(content=f"""<!DOCTYPE html>
<html>
<head>
    <title>IMDB Service</title>
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
    <h1>🎬 IMDB Service</h1>
    <p>A caching service for IMDB public dataset metadata with daily updates and pre-computed charts.</p>

    <h2>API Endpoints</h2>

    <div class="endpoint">
        <strong>GET /stats</strong> - Service status and table row counts<br>
        <code>curl {base}/stats</code>
    </div>

    <div class="endpoint">
        <strong>GET /title/{{imdb_id}}</strong> - Full title record by IMDb ID<br>
        <code>curl {base}/title/tt0111161</code>
    </div>

    <div class="endpoint">
        <strong>GET /person/{{imdb_id}}</strong> - Person record by IMDb person ID<br>
        <code>curl {base}/person/nm0000199</code>
    </div>

    <div class="endpoint">
        <strong>GET /chart/{{chart_name}}</strong> - Pre-computed ranked chart<br>
        <code>curl "{base}/chart/top_movies?limit=10"</code><br>
        Available charts: top_movies, top_shows, lowest_rated, top_english, top_indian, top_tamil, top_telugu, top_malayalam
    </div>

    <div class="endpoint">
        <strong>GET /search</strong> - Filtered title search returning IMDb IDs<br>
        <code>curl "{base}/search?type=movie&amp;rating.gte=8&amp;limit=10"</code>
    </div>

    <div class="endpoint">
        <strong>GET /ratings/{{imdb_id}}</strong> - Return rating metadata for a title<br>
        <code>curl "{base}/ratings/tt0111161"</code>
    </div>

    <div class="endpoint">
        <strong>GET /genre/{{imdb_id}}</strong> - Return genres for a title<br>
        <code>curl "{base}/genre/tt0111161"</code>
    </div>

    <div class="endpoint">
        <strong>GET /episode-rating/{{parent_imdb_id}}?season={{int}}&amp;episode={{int}}</strong> - Rating for one episode<br>
        <code>curl "{base}/episode-rating/tt0096697?season=5&amp;episode=12"</code>
    </div>

    <div class="endpoint">
        <strong>GET /episode-ratings/{{parent_imdb_id}}?season={{int}}</strong> - All episode ratings for a show<br>
        <code>curl "{base}/episode-ratings/tt0096697"</code>
    </div>

    <div class="endpoint">
        <strong>GET /parental/{{imdb_id}}</strong> - Cached IMDb parental guide severities<br>
        <code>curl "{base}/parental/tt0111161"</code>
    </div>

    <h2>API Documentation</h2>

    <div class="endpoint">
        <strong><a href="{base}/docs">Swagger UI</a></strong> - Interactive API documentation<br>
        Try out endpoints directly from your browser
    </div>

    <div class="endpoint">
        <strong><a href="{base}/redoc">ReDoc</a></strong> - Alternative API documentation<br>
        Clean, readable documentation format
    </div>

    <div class="endpoint">
        <strong><a href="{base}/openapi.json">OpenAPI Schema</a></strong> - Machine-readable API specification<br>
        JSON schema for automated tools and clients
    </div>
</body>
</html>
""")


@app.post("/refresh/{table}")
async def refresh_table(table: str) -> Dict[str, str]:
    """Trigger a manual re-download and re-import of a single table's dataset."""
    if table not in ALLOWED_TABLES:
        raise HTTPException(status_code=404, detail=f"Unknown table: {table!r}")
    if _refresh_lock.locked() or current_phase != "idle":
        raise HTTPException(status_code=409, detail="A refresh is already in progress")

    stem = TABLE_TO_STEM[table]
    asyncio.create_task(_refresh_single_table(stem))
    return {"status": "started", "table": table}


@app.get("/", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    """Return a visual stats dashboard with per-table refresh controls."""
    from importer import DATASET_FILES, DATASET_REFRESH_DAYS, MIN_ROWS, STEM_TO_TABLE

    # Use a relative base (ROOT_PATH only) so fetches inherit the page's
    # scheme/host. Building an absolute URL from request.url.scheme yields
    # "http" behind a TLS-terminating proxy and triggers mixed-content blocks.
    base = ROOT_PATH

    table_meta = {
        STEM_TO_TABLE[stem]: {
            "stem": stem,
            "file": DATASET_FILES[stem],
            "refresh_days": DATASET_REFRESH_DAYS.get(stem, 0),
            "min_rows": MIN_ROWS.get(STEM_TO_TABLE[stem], 0),
        }
        for stem in DATASET_FILES
    }

    return HTMLResponse(content=f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>IMDB Service Dashboard</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            margin: 0; padding: 24px; background: #0f1419; color: #e6e6e6;
        }}
        .wrap {{ max-width: 1100px; margin: 0 auto; }}
        header {{
            display: flex; align-items: baseline; justify-content: space-between;
            flex-wrap: wrap; gap: 12px; margin-bottom: 24px;
        }}
        h1 {{ margin: 0; font-size: 24px; color: #f5c518; }}
        a {{ color: #5ab0ff; text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
        .statusbar {{
            display: flex; gap: 24px; flex-wrap: wrap; background: #1a2029;
            padding: 16px; border-radius: 8px; margin-bottom: 24px;
        }}
        .status-item {{ font-size: 13px; }}
        .status-item .label {{ color: #8a94a6; display: block; margin-bottom: 4px; }}
        .badge {{
            display: inline-block; padding: 3px 10px; border-radius: 12px;
            font-size: 12px; font-weight: 600; text-transform: uppercase;
        }}
        .badge.idle {{ background: #2d4a2d; color: #7fd97f; }}
        .badge.downloading {{ background: #3a3a1a; color: #e0d94f; }}
        .badge.importing {{ background: #1a3a4a; color: #5ab0ff; }}
        .badge.building_charts {{ background: #3a1a4a; color: #c07fff; }}
        h2 {{ font-size: 16px; color: #b8c0cc; border-bottom: 1px solid #2a323d;
             padding-bottom: 8px; margin-top: 32px; }}
        .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
                gap: 16px; }}
        .card {{ background: #1a2029; border-radius: 8px; padding: 16px; }}
        .card h3 {{ margin: 0 0 4px; font-size: 15px; color: #fff; }}
        .card .stem {{ font-size: 12px; color: #8a94a6; margin-bottom: 12px; }}
        .rowcount {{ font-size: 22px; font-weight: 700; color: #f5c518; }}
        .meta {{ font-size: 12px; color: #8a94a6; margin: 8px 0; line-height: 1.6; }}
        .bar {{ height: 6px; background: #2a323d; border-radius: 3px; overflow: hidden;
               margin: 8px 0; }}
        .bar > span {{ display: block; height: 100%; background: #7fd97f; }}
        .bar.low > span {{ background: #e05f5f; }}
        .bar.warn > span {{ background: #e0d94f; }}
        button {{
            background: #f5c518; color: #0f1419; border: none; padding: 8px 14px;
            border-radius: 6px; font-weight: 600; cursor: pointer; font-size: 13px;
            margin-top: 8px;
        }}
        button:hover {{ background: #ffd633; }}
        button:disabled {{ background: #3a3f47; color: #6a707a; cursor: not-allowed; }}
        .tbl-status {{ font-size: 12px; margin-top: 8px; height: 16px; }}
        ul {{ list-style: none; padding: 0; margin: 0; columns: 2; }}
        li {{ font-size: 13px; padding: 4px 0; }}
        .count {{ color: #8a94a6; }}
    </style>
</head>
<body>
    <div class="wrap">
        <header>
            <h1>🎬 IMDB Service Dashboard</h1>
            <a href="{base}/endpoints">View API Documentation →</a>
        </header>

        <div class="statusbar">
            <div class="status-item">
                <span class="label">Phase</span>
                <span id="phase" class="badge idle">–</span>
            </div>
            <div class="status-item">
                <span class="label">Status</span>
                <span id="svc-status">–</span>
            </div>
            <div class="status-item">
                <span class="label">Last Refresh</span>
                <span id="last-refresh">–</span>
            </div>
            <div class="status-item">
                <span class="label">Last Activity</span>
                <span id="last-activity">–</span>
            </div>
        </div>

        <h2>Tables</h2>
        <div class="grid" id="tables"></div>

        <h2>Charts</h2>
        <ul id="charts"><li>Loading…</li></ul>

        <h2>Parental Cache</h2>
        <ul id="parental"><li>Loading…</li></ul>
    </div>

    <script>
    const TABLE_META = {json.dumps(table_meta)};
    const BASE = {json.dumps(base)};
    let polling = null;

    function fmt(n) {{ return n == null ? '–' : Number(n).toLocaleString(); }}
    function fmtTime(t) {{ return t ? new Date(t).toLocaleString() : '–'; }}

    function renderTables(counts, progress, updated) {{
        const grid = document.getElementById('tables');
        grid.innerHTML = '';
        updated = updated || {{}};
        for (const [table, meta] of Object.entries(TABLE_META)) {{
            const rows = counts[table];
            const prog = (progress || {{}})[table];
            const pct = meta.min_rows ? Math.min(100, (rows / meta.min_rows) * 100) : 100;
            let barCls = 'bar';
            if (rows != null && meta.min_rows) {{
                if (rows < meta.min_rows) barCls = 'bar low';
                else if (rows < meta.min_rows * 1.1) barCls = 'bar warn';
            }}
            let statusTxt = '';
            if (prog) {{
                statusTxt = prog.status === 'done'
                    ? '✅ ' + fmt(prog.rows) + ' rows'
                    : '⏳ ' + prog.status + (prog.rows ? ' — ' + fmt(prog.rows) : '');
            }}
            const busy = !!polling;
            grid.innerHTML += `
                <div class="card">
                    <h3>${{table}}</h3>
                    <div class="stem">${{meta.stem}}</div>
                    <div class="rowcount">${{fmt(rows)}}</div>
                    <div class="${{barCls}}"><span style="width:${{pct}}%"></span></div>
                    <div class="meta">
                        Source: ${{meta.file}}<br>
                        Refresh interval: ${{meta.refresh_days}} day(s)<br>
                        Min rows: ${{fmt(meta.min_rows)}}<br>
                        Updated: ${{fmtTime(updated[table])}}
                    </div>
                    <button onclick="refresh('${{table}}')" ${{busy ? 'disabled' : ''}}>↻ Refresh</button>
                    <div class="tbl-status">${{statusTxt}}</div>
                </div>`;
        }}
    }}

    function renderCharts(names, progress) {{
        const el = document.getElementById('charts');
        if (progress && progress.total > 0) {{
            const pct = Math.min(100, Math.round((progress.completed / progress.total) * 100));
            el.innerHTML = `
                <li>⏳ Rebuilding charts: ${{progress.completed}} / ${{progress.total}} (${{pct}}%)</li>
                <li>Current: <code>${{progress.current}}</code></li>
            `;
            return;
        }}
        if (!names || !names.length) {{ el.innerHTML = '<li>No charts cached</li>'; return; }}
        el.innerHTML = names.map(n => `<li>${{n}}</li>`).join('');
    }}

    function renderParental(pc) {{
        const el = document.getElementById('parental');
        if (!pc) {{ el.innerHTML = '<li>No data</li>'; return; }}
        const items = [`<li>items_cached: <span class="count">${{fmt(pc.items_cached)}}</span></li>`];
        if (pc.flag_counts && typeof pc.flag_counts === 'object') {{
            Object.entries(pc.flag_counts).forEach(([k, v]) => {{
                items.push(`<li>${{k}}: <span class="count">${{fmt(v)}}</span></li>`);
            }});
        }}
        el.innerHTML = items.join('');
    }}

    async function load() {{
        const r = await fetch(BASE + '/stats');
        const d = await r.json();
        const phaseEl = document.getElementById('phase');
        phaseEl.textContent = d.phase || '–';
        phaseEl.className = 'badge ' + (d.phase || 'idle');
        document.getElementById('svc-status').textContent = d.status || '–';
        document.getElementById('last-refresh').textContent = fmtTime(d.last_refresh);
        document.getElementById('last-activity').textContent = fmtTime(d.last_activity);
        renderTables(d.table_counts || {{}}, d.import_progress, d.table_updated);
        renderCharts(d.charts_cached, d.chart_progress);
        renderParental(d.parental_cache);

        if (d.phase && d.phase !== 'idle') {{
            if (!polling) polling = setInterval(load, 2000);
        }} else if (polling) {{
            clearInterval(polling); polling = null;
            renderTables(d.table_counts || {{}}, null, d.table_updated);
        }}
    }}

    async function refresh(table) {{
        const r = await fetch(BASE + '/refresh/' + table, {{ method: 'POST' }});
        if (r.status === 409) {{ alert('A refresh is already in progress'); return; }}
        if (!r.ok) {{ alert('Refresh failed: ' + r.status); return; }}
        if (!polling) polling = setInterval(load, 2000);
        load();
    }}

    load();
    </script>
</body>
</html>
""")


@app.get("/stats")
async def get_stats() -> Dict[str, Any]:
    """Return service health: status, phase, progress indicators, and per-table row counts."""
    if not _db_is_ready():
        return {
            "status": "initializing",
            "phase": current_phase,
            "download_progress": download_progress,
            "import_progress": import_progress,
            "chart_progress": chart_progress,
            "last_activity": last_activity,
        }

    try:
        parental_cache = await _get_parental_cache_stats()
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("SELECT value FROM import_meta WHERE key = 'row_counts'")
            row = await cursor.fetchone()
            counts: Dict[str, Any] = json.loads(row[0]) if row else {}

            cursor = await db.execute("SELECT value FROM import_meta WHERE key = 'table_updated'")
            row = await cursor.fetchone()
            table_updated: Dict[str, Any] = json.loads(row[0]) if row and row[0] else {}

        return {
            "status": "online",
            "phase": current_phase,
            "last_refresh": last_refresh,
            "last_activity": last_activity,
            "table_counts": counts,
            "table_updated": table_updated,
            "parental_cache": parental_cache,
            "charts_cached": list(charts.chart_cache.keys()),
            "download_progress": download_progress,
            "import_progress": import_progress,
            "chart_progress": chart_progress,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/ratings/{imdb_id}")
async def get_ratings(imdb_id: str) -> Dict[str, Any]:
    """Return the IMDb rating metadata for a single title."""
    if not _db_is_ready():
        raise HTTPException(status_code=503, detail="Service initializing")

    field = "ratings"
    sql = """
        SELECT tb.tconst, tb.primaryTitle, tb.titleType, tr.averageRating, tr.numVotes
        FROM title_basics tb
        LEFT JOIN title_ratings tr ON tb.tconst = tr.tconst
        WHERE tb.tconst = ?
    """

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(sql, (imdb_id,))
            row = await cursor.fetchone()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ratings error: {e}")

    if not row:
        raise HTTPException(status_code=404, detail=f"Title {imdb_id!r} not found")

    result = _normalize_extract_row(field, row)
    return {
        "field": field,
        "imdb_id": imdb_id,
        "result": result,
    }


@app.get("/genre/{imdb_id}")
async def get_genres(imdb_id: str) -> Dict[str, Any]:
    """Return the IMDb genres for a single title."""
    if not _db_is_ready():
        raise HTTPException(status_code=503, detail="Service initializing")

    field = "genres"
    sql = """
        SELECT tb.tconst, tb.primaryTitle, tb.titleType, tb.genres
        FROM title_basics tb
        LEFT JOIN title_ratings tr ON tb.tconst = tr.tconst
        WHERE tb.tconst = ?
    """

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(sql, (imdb_id,))
            row = await cursor.fetchone()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Genre error: {e}")

    if not row:
        raise HTTPException(status_code=404, detail=f"Title {imdb_id!r} not found")

    result = _normalize_extract_row(field, row)
    return {
        "field": field,
        "imdb_id": imdb_id,
        "result": result,
    }


@app.get("/episode-rating/{parent_imdb_id}")
async def get_episode_rating(
    parent_imdb_id: str,
    season: int,
    episode: int,
) -> Dict[str, Any]:
    """Return rating metadata for a single episode of a series."""
    if not _db_is_ready():
        raise HTTPException(status_code=503, detail="Service initializing")

    sql = """
        SELECT te.tconst, te.parentTconst, te.seasonNumber, te.episodeNumber,
               tr.averageRating, tr.numVotes
        FROM title_episode te
        LEFT JOIN title_ratings tr ON te.tconst = tr.tconst
        WHERE te.parentTconst = ? AND te.seasonNumber = ? AND te.episodeNumber = ?
    """

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(sql, (parent_imdb_id, season, episode))
            row = await cursor.fetchone()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Episode rating error: {e}")

    if not row:
        raise HTTPException(
            status_code=404,
            detail=f"Episode not found for {parent_imdb_id} S{season:02d}E{episode:02d}",
        )

    return {
        "tconst": row["tconst"],
        "parentTconst": row["parentTconst"],
        "seasonNumber": row["seasonNumber"],
        "episodeNumber": row["episodeNumber"],
        "averageRating": row["averageRating"],
        "numVotes": row["numVotes"],
    }


@app.get("/episode-ratings/{parent_imdb_id}")
async def get_episode_ratings(
    parent_imdb_id: str,
    season: Optional[int] = None,
) -> Dict[str, Any]:
    """Return all episode ratings for a series, optionally filtered to one season."""
    if not _db_is_ready():
        raise HTTPException(status_code=503, detail="Service initializing")

    params: list = [parent_imdb_id]
    sql = """
        SELECT te.tconst, te.seasonNumber, te.episodeNumber,
               tr.averageRating, tr.numVotes
        FROM title_episode te
        LEFT JOIN title_ratings tr ON te.tconst = tr.tconst
        WHERE te.parentTconst = ?
    """
    if season is not None:
        sql += " AND te.seasonNumber = ?"
        params.append(season)

    sql += " ORDER BY te.seasonNumber, te.episodeNumber"

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(sql, params)
            rows = await cursor.fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Episode ratings error: {e}")

    if season is not None:
        episodes: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            ep_num = str(row["episodeNumber"])
            episodes[ep_num] = {
                "tconst": row["tconst"],
                "averageRating": row["averageRating"],
                "numVotes": row["numVotes"],
            }
        return {
            "parentTconst": parent_imdb_id,
            "season": season,
            "total_episodes": len(episodes),
            "episodes": episodes,
        }

    seasons: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for row in rows:
        season_num = str(row["seasonNumber"])
        ep_num = str(row["episodeNumber"])
        seasons.setdefault(season_num, {})[ep_num] = {
            "tconst": row["tconst"],
            "averageRating": row["averageRating"],
            "numVotes": row["numVotes"],
        }

    return {
        "parentTconst": parent_imdb_id,
        "total_episodes": sum(len(eps) for eps in seasons.values()),
        "seasons": seasons,
    }


@app.get("/parental/{imdb_id}")
async def get_parental_guide(
    imdb_id: str,
    ignore_cache: bool = False,
) -> Dict[str, Any]:
    """Return cached IMDb parental-guide labels for a title, refreshing when stale."""
    if not _db_is_ready():
        raise HTTPException(status_code=503, detail="Service initializing")

    _parental_log("endpoint_start", imdb_id, ignore_cache=ignore_cache)
    cached, expired, cached_at = (
        (None, None, None)
        if ignore_cache
        else await _query_parental_cache(imdb_id)
    )
    if cached and expired is False:
        _parental_log("endpoint_cache_hit", imdb_id, cached_at=cached_at)
        return {
            "imdb_id": imdb_id,
            "cached": True,
            "cached_at": cached_at,
            "cache_age_days": _parental_cache_age_days(cached_at),
            "ttl_days": PARENTAL_GUIDE_TTL_DAYS,
            "parental_guide": cached,
        }
    if cached:
        _parental_log("endpoint_cache_stale", imdb_id, expired=expired, cached_at=cached_at)
    else:
        _parental_log("endpoint_cache_miss", imdb_id)

    html_text = await _fetch_parental_guide_html(imdb_id)
    parental = _parse_parental_guide_html(html_text)
    cached_at = await _update_parental_cache(imdb_id, parental)
    _parental_log(
        "endpoint_cache_updated", imdb_id, categories=",".join(sorted(parental.keys()))
    )
    return {
        "imdb_id": imdb_id,
        "cached": False,
        "cached_at": cached_at,
        "cache_age_days": 0,
        "ttl_days": PARENTAL_GUIDE_TTL_DAYS,
        "parental_guide": parental,
    }


@app.get("/title/{imdb_id}")
async def get_title(imdb_id: str) -> Dict[str, Any]:
    """Return full title record by IMDb ID (e.g. tt0111161)."""
    if not _db_is_ready():
        raise HTTPException(status_code=503, detail="Service initializing")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            """
            SELECT tb.*, tr.averageRating, tr.numVotes
            FROM title_basics tb
            LEFT JOIN title_ratings tr ON tb.tconst = tr.tconst
            WHERE tb.tconst = ?
            """,
            (imdb_id,),
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Title {imdb_id!r} not found")

        result = dict(row)

        cursor = await db.execute(
            "SELECT directors, writers FROM title_crew WHERE tconst = ?", (imdb_id,)
        )
        crew = await cursor.fetchone()
        result["directors"] = crew["directors"] if crew else None
        result["writers"] = crew["writers"] if crew else None

        cursor = await db.execute(
            """
            SELECT nconst, ordering, category, job, characters
            FROM title_principals WHERE tconst = ? ORDER BY ordering
            """,
            (imdb_id,),
        )
        principals = await cursor.fetchall()
        result["principals"] = [dict(p) for p in principals]

        if result.get("titleType") in ("tvSeries", "tvMiniSeries"):
            cursor = await db.execute(
                "SELECT COUNT(*) FROM title_episode WHERE parentTconst = ?", (imdb_id,)
            )
            ep_row = await cursor.fetchone()
            result["episode_count"] = ep_row[0] if ep_row else 0

    return result


@app.get("/chart/{chart_name}")
async def get_chart(chart_name: str, limit: Optional[int] = None) -> Dict[str, Any]:
    """Return a pre-computed ranked chart of IMDb titles."""
    if chart_name not in charts.CHART_CONFIGS:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown chart: {chart_name!r}. Valid: {list(charts.CHART_CONFIGS.keys())}",
        )

    if not _db_is_ready() and not charts.chart_cache:
        raise HTTPException(status_code=503, detail="Service initializing")

    if limit is not None and (limit < 1 or limit > charts.MAX_CHART_SIZE):
        raise HTTPException(status_code=400, detail=f"limit must be ≤ {charts.MAX_CHART_SIZE}")

    results = charts.chart_cache.get(chart_name, [])
    if limit is not None:
        results = results[:limit]
    else:
        results = results[: charts.DEFAULT_CHART_SIZE]

    return {"chart": chart_name, "total": len(results), "results": results}


@app.get("/person/{imdb_id}")
async def get_person(imdb_id: str) -> Dict[str, Any]:
    """Return person record by IMDb person ID (e.g. nm0000093)."""
    if not _db_is_ready():
        raise HTTPException(status_code=503, detail="Service initializing")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM name_basics WHERE nconst = ?", (imdb_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Person {imdb_id!r} not found")
        return dict(row)
