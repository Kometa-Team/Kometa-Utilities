"""IMDB dataset download and SQLite import."""

import asyncio
import gzip
import json
import sqlite3
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS title_basics (
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
CREATE INDEX IF NOT EXISTS idx_tb_type ON title_basics(titleType);
CREATE INDEX IF NOT EXISTS idx_tb_year ON title_basics(startYear);
CREATE INDEX IF NOT EXISTS idx_tb_genres ON title_basics(genres);

CREATE TABLE IF NOT EXISTS title_ratings (
    tconst TEXT PRIMARY KEY,
    averageRating REAL,
    numVotes INTEGER
);

CREATE TABLE IF NOT EXISTS title_akas (
    tconst TEXT NOT NULL,
    ordering INTEGER,
    title TEXT,
    region TEXT,
    language TEXT,
    types TEXT,
    attributes TEXT,
    isOriginalTitle INTEGER
);
CREATE INDEX IF NOT EXISTS idx_aka_tconst ON title_akas(tconst);
CREATE INDEX IF NOT EXISTS idx_aka_region ON title_akas(region);
CREATE INDEX IF NOT EXISTS idx_aka_language ON title_akas(language);
CREATE INDEX IF NOT EXISTS idx_aka_lang_tconst ON title_akas(language, tconst);
CREATE INDEX IF NOT EXISTS idx_aka_region_tconst ON title_akas(region, tconst);

CREATE TABLE IF NOT EXISTS title_crew (
    tconst TEXT PRIMARY KEY,
    directors TEXT,
    writers TEXT
);

CREATE TABLE IF NOT EXISTS title_episode (
    tconst TEXT PRIMARY KEY,
    parentTconst TEXT,
    seasonNumber INTEGER,
    episodeNumber INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ep_parent ON title_episode(parentTconst);
CREATE INDEX IF NOT EXISTS idx_ep_parent_season_episode ON title_episode(parentTconst, seasonNumber, episodeNumber);

CREATE TABLE IF NOT EXISTS title_principals (
    tconst TEXT NOT NULL,
    ordering INTEGER,
    nconst TEXT NOT NULL,
    category TEXT,
    job TEXT,
    characters TEXT
);
CREATE INDEX IF NOT EXISTS idx_pr_tconst ON title_principals(tconst);
CREATE INDEX IF NOT EXISTS idx_pr_nconst ON title_principals(nconst);

CREATE TABLE IF NOT EXISTS name_basics (
    nconst TEXT PRIMARY KEY,
    primaryName TEXT,
    birthYear INTEGER,
    deathYear INTEGER,
    primaryProfession TEXT,
    knownForTitles TEXT
);
CREATE INDEX IF NOT EXISTS idx_nb_name ON name_basics(primaryName);

CREATE TABLE IF NOT EXISTS import_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

"""


def create_schema(conn: sqlite3.Connection) -> None:
    """Create all dataset tables and indexes in the given connection."""
    conn.executescript(SCHEMA_SQL)


# Minimum row counts per dataset (guards against truncated downloads)
MIN_ROWS: dict[str, int] = {
    "title_basics": 1_000_000,
    "title_ratings": 500_000,
    "title_akas": 1_000_000,
    "title_crew": 1_000_000,
    "title_episode": 100_000,
    "title_principals": 1_000_000,
    "name_basics": 1_000_000,
}

BATCH_SIZE = 10_000


def _null(value: str) -> Optional[str]:
    """Convert IMDB null sentinel to Python None."""
    return None if value == r"\N" else value


def _int_or_none(value: str) -> Optional[int]:
    v = _null(value)
    return int(v) if v is not None else None


def _real_or_none(value: str) -> Optional[float]:
    v = _null(value)
    return float(v) if v is not None else None


INT_COLS = frozenset(
    {
        "isAdult",
        "startYear",
        "endYear",
        "runtimeMinutes",
        "numVotes",
        "ordering",
        "isOriginalTitle",
        "seasonNumber",
        "episodeNumber",
        "birthYear",
        "deathYear",
    }
)
REAL_COLS = frozenset({"averageRating"})


def _coerce(col: str, val: str):
    if col in INT_COLS:
        return _int_or_none(val)
    if col in REAL_COLS:
        return _real_or_none(val)
    return _null(val)


def import_table(
    conn: sqlite3.Connection,
    gz_path: Path,
    table: str,
    columns: list[str],
    min_rows: int,
    on_progress: Optional[Callable[[int], None]] = None,
) -> int:
    """
    Parse a gzip TSV file and bulk-insert into the given table.

    Does NOT manage transactions — the caller is responsible for BEGIN/COMMIT/ROLLBACK.
    Validates that at least min_rows were inserted.

    Returns the number of rows inserted.
    """
    placeholders = ",".join("?" * len(columns))
    sql = f"INSERT OR REPLACE INTO {table} VALUES ({placeholders})"  # nosec B608

    count = 0
    batch: list = []
    with gzip.open(gz_path, "rt", encoding="utf-8") as f:
        f.readline()  # skip header row
        for line in f:
            parts = line.rstrip("\n").split("\t")
            row = tuple(_coerce(col, val) for col, val in zip(columns, parts))
            batch.append(row)
            if len(batch) >= BATCH_SIZE:
                conn.executemany(sql, batch)
                count += len(batch)
                batch = []
                if on_progress:
                    on_progress(count)
        if batch:
            conn.executemany(sql, batch)
            count += len(batch)

    if count < min_rows:
        raise ValueError(
            f"import_table({table}): too few rows — got {count}, expected ≥ {min_rows}"
        )

    return count


IMDB_BASE_URL = "https://datasets.imdbws.com"
DATASET_MANIFEST = "dataset_manifest.json"

DATASET_REFRESH_DAYS: dict[str, int] = {
    "title.ratings": 1,
    "title.basics": 2,
    "title.episode": 2,
    "title.akas": 7,
    "title.crew": 7,
    "title.principals": 7,
    "name.basics": 7,
}

DATASET_FILES: dict[str, str] = {
    "title.basics": "title.basics.tsv.gz",
    "title.ratings": "title.ratings.tsv.gz",
    "title.akas": "title.akas.tsv.gz",
    "title.crew": "title.crew.tsv.gz",
    "title.episode": "title.episode.tsv.gz",
    "title.principals": "title.principals.tsv.gz",
    "name.basics": "name.basics.tsv.gz",
}


def _load_manifest(data_dir: Path) -> dict[str, Any]:
    manifest_path = data_dir / DATASET_MANIFEST
    if not manifest_path.exists():
        return {}
    try:
        loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {str(k): v for k, v in loaded.items()}


def _save_manifest(data_dir: Path, manifest: dict[str, Any]) -> None:
    (data_dir / DATASET_MANIFEST).write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _dataset_due(manifest: dict[str, Any], stem: str) -> bool:
    entry = manifest.get(stem)
    if not entry or not entry.get("last_checked"):
        return True
    refresh_days = DATASET_REFRESH_DAYS.get(stem, 1)
    try:
        last_checked = datetime.fromisoformat(entry["last_checked"])
    except ValueError:
        return True
    if last_checked.tzinfo is None:
        last_checked = last_checked.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last_checked >= timedelta(days=refresh_days)


def _metadata_changed(previous: Optional[dict[str, Any]], current: dict[str, Any]) -> bool:
    if not previous:
        return True
    for key in ("etag", "last_modified", "content_length"):
        if (previous.get(key) or None) != (current.get(key) or None):
            return True
    return False


async def _fetch_remote_metadata(
    client: httpx.AsyncClient, filename: str
) -> dict[str, Optional[str]]:
    url = f"{IMDB_BASE_URL}/{filename}"
    response = await client.head(url, timeout=120.0)
    if response.status_code == 405:
        async with client.stream("GET", url, timeout=120.0) as get_response:
            get_response.raise_for_status()
            headers = get_response.headers
            return {
                "etag": headers.get("etag"),
                "last_modified": headers.get("last-modified"),
                "content_length": headers.get("content-length"),
            }
    response.raise_for_status()
    headers = response.headers
    return {
        "etag": headers.get("etag"),
        "last_modified": headers.get("last-modified"),
        "content_length": headers.get("content-length"),
    }


def _gzip_is_complete(path: Path) -> bool:
    """Return True if the gzip file exists and its stream ends cleanly."""
    if not path.exists():
        return False
    try:
        with gzip.open(path, "rb") as f:
            while f.read(65536):
                pass
        return True
    except Exception:
        return False


async def _download_one(
    client: httpx.AsyncClient,
    stem: str,
    filename: str,
    dest: Path,
    manifest: dict[str, Any],
    on_start: Optional[Callable[[str], None]] = None,
    on_done: Optional[Callable[[str], None]] = None,
) -> tuple[bool, dict[str, Any]]:
    """Refresh a single dataset file if metadata changed or the local gzip is incomplete."""
    now_iso = datetime.now(timezone.utc).isoformat()
    previous = manifest.get(stem, {})
    file_complete = await asyncio.to_thread(_gzip_is_complete, dest)
    if file_complete and not _dataset_due(manifest, stem):
        print(f"⏭️  Skipping {filename} (not due for refresh)")
        updated = {
            **previous,
            "last_checked": previous.get("last_checked", now_iso),
        }
        return False, updated

    remote_metadata = await _fetch_remote_metadata(client, filename)
    metadata_changed = _metadata_changed(previous, remote_metadata)
    if file_complete and not metadata_changed:
        print(f"⏭️  Skipping {filename} (remote metadata unchanged)")
        updated = {
            **previous,
            **remote_metadata,
            "last_checked": now_iso,
        }
        return False, updated

    url = f"{IMDB_BASE_URL}/{filename}"
    if on_start:
        on_start(filename)
    print(f"⬇️  Downloading {filename}...")
    async with client.stream("GET", url, timeout=600.0) as response:
        response.raise_for_status()
        with dest.open("wb") as f:
            async for chunk in response.aiter_bytes(65536):
                f.write(chunk)
    print(f"✅ Downloaded {filename}")
    if on_done:
        on_done(filename)
    return True, {
        **remote_metadata,
        "last_checked": now_iso,
        "last_downloaded": now_iso,
    }


async def download_datasets(
    data_dir: Path,
    on_file_start: Optional[Callable[[str], None]] = None,
    on_file_done: Optional[Callable[[str], None]] = None,
    stems: Optional[list[str]] = None,
) -> tuple[dict[str, Path], list[str]]:
    """
    Download IMDB dataset files concurrently to data_dir.

    If stems is provided, only download those datasets. Otherwise download all 7.

    Returns a tuple of:
      - dict mapping stem → local Path
      - list of stems whose local files changed this run
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    targets = {
        stem: filename for stem, filename in DATASET_FILES.items() if stems is None or stem in stems
    }
    paths: dict[str, Path] = {stem: data_dir / filename for stem, filename in targets.items()}
    manifest = _load_manifest(data_dir)

    async with httpx.AsyncClient(timeout=600.0) as client:
        tasks = [
            _download_one(
                client, stem, filename, paths[stem], manifest, on_file_start, on_file_done
            )
            for stem, filename in targets.items()
        ]
        results = await asyncio.gather(*tasks)

    changed_stems: list[str] = []
    for stem, (changed, updated_manifest) in zip(targets.keys(), results):
        manifest[stem] = updated_manifest
        if changed:
            changed_stems.append(stem)

    _save_manifest(data_dir, manifest)

    return paths, changed_stems


# Maps dataset stem → table name
STEM_TO_TABLE: dict[str, str] = {
    "title.basics": "title_basics",
    "title.ratings": "title_ratings",
    "title.akas": "title_akas",
    "title.crew": "title_crew",
    "title.episode": "title_episode",
    "title.principals": "title_principals",
    "name.basics": "name_basics",
}

# Reverse mapping: table name → dataset stem
TABLE_TO_STEM: dict[str, str] = {v: k for k, v in STEM_TO_TABLE.items()}

# Column definitions per table (must match TSV file column order)
TABLE_COLUMNS: dict[str, list[str]] = {
    "title_basics": [
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
    "title_ratings": ["tconst", "averageRating", "numVotes"],
    "title_akas": [
        "tconst",
        "ordering",
        "title",
        "region",
        "language",
        "types",
        "attributes",
        "isOriginalTitle",
    ],
    "title_crew": ["tconst", "directors", "writers"],
    "title_episode": ["tconst", "parentTconst", "seasonNumber", "episodeNumber"],
    "title_principals": ["tconst", "ordering", "nconst", "category", "job", "characters"],
    "name_basics": [
        "nconst",
        "primaryName",
        "birthYear",
        "deathYear",
        "primaryProfession",
        "knownForTitles",
    ],
}

ALLOWED_TABLES = frozenset(TABLE_COLUMNS)


def _delete_table(conn: sqlite3.Connection, table: str) -> None:
    """Delete all rows from a validated internal table name."""
    if table not in ALLOWED_TABLES:
        raise ValueError(f"Unexpected table: {table}")
    conn.execute(f"DELETE FROM {table}")  # nosec B608 - table name validated against ALLOWED_TABLES


def run_direct_import(
    gz_paths: dict[str, Path],
    live_db: Path,
    changed_stems: Optional[list[str]] = None,
    min_rows_override: int = 0,
    on_table_start: Optional[Callable[[str], None]] = None,
    on_table_done: Optional[Callable[[str, int], None]] = None,
    on_table_progress: Optional[Callable[[str, int], None]] = None,
) -> None:
    """
    Import dataset files directly into the live DB using WAL mode.

    Uses BEGIN IMMEDIATE so readers see the pre-import state until COMMIT.
    On failure the transaction is rolled back and the live DB is untouched.

    gz_paths: dict mapping dataset stem → local .tsv.gz path
    live_db: path to the live SQLite DB
    """
    import_stems = changed_stems if changed_stems is not None else list(gz_paths.keys())
    conn = sqlite3.connect(live_db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    create_schema(conn)

    existing_counts: dict[str, int] = {}
    cursor = conn.execute("SELECT value FROM import_meta WHERE key = 'row_counts'")
    row = cursor.fetchone()
    if row and row[0]:
        try:
            existing_counts = json.loads(row[0])
        except json.JSONDecodeError:
            existing_counts = {}

    existing_updated: dict[str, str] = {}
    cursor = conn.execute("SELECT value FROM import_meta WHERE key = 'table_updated'")
    row = cursor.fetchone()
    if row and row[0]:
        try:
            existing_updated = json.loads(row[0])
        except json.JSONDecodeError:
            existing_updated = {}

    row_counts: dict[str, int] = existing_counts.copy()
    table_updated: dict[str, str] = existing_updated.copy()

    try:
        conn.execute("BEGIN IMMEDIATE")
        for stem in import_stems:
            gz_path = gz_paths[stem]
            table = STEM_TO_TABLE.get(stem)
            if table is None:
                print(f"Unknown stem {stem!r}, skipping")
                continue
            columns = TABLE_COLUMNS[table]
            min_rows = (
                min_rows_override if min_rows_override is not None else MIN_ROWS.get(table, 0)
            )
            print(f"Direct-importing {stem} -> {table}...")
            if on_table_start:
                on_table_start(table)
            _delete_table(conn, table)
            _t, _cb = table, on_table_progress
            _progress_cb = (lambda t, cb: lambda n: cb(t, n))(_t, _cb) if _cb else None
            count = import_table(conn, gz_path, table, columns, min_rows, _progress_cb)
            row_counts[table] = count
            table_updated[table] = datetime.now(timezone.utc).isoformat()
            if on_table_done:
                on_table_done(table, count)
            print(f"   {count:,} rows")

        conn.execute(
            "INSERT OR REPLACE INTO import_meta VALUES (?, ?)",
            ("last_refresh", datetime.now(timezone.utc).isoformat()),
        )
        conn.execute(
            "INSERT OR REPLACE INTO import_meta VALUES (?, ?)",
            ("row_counts", json.dumps(row_counts)),
        )
        conn.execute(
            "INSERT OR REPLACE INTO import_meta VALUES (?, ?)",
            ("table_updated", json.dumps(table_updated)),
        )
        conn.execute("COMMIT")
        print("Direct import complete")

    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:  # nosec B110
            pass
        traceback.print_exc()
        raise
    finally:
        conn.close()
