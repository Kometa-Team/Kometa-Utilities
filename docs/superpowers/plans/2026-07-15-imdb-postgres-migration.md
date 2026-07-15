# IMDB Service — Postgres Migration Plan

Date: 2026-07-15
Status: Draft / Not scheduled

## Motivation

The WAL direct-import fix (2026-07-15) solved the immediate disk-pressure problem. Postgres addresses different concerns:

- **No file-level locking** — multiple processes can read/write concurrently (relevant if scaling beyond one Uvicorn worker)
- **Managed backups** — RDS snapshots, point-in-time recovery, no `VACUUM` worries
- **Connection pooling** — PgBouncer or RDS proxy handles burst traffic without opening 14 connections per request
- **Remote DB** — data lives on the managed service, not the app server; the container can be stateless

## Current Architecture (SQLite)

### Schema — 8 tables

| Table | Source | Estimated Rows |
|-------|--------|----------------|
| `title_basics` | `title.basics.tsv.gz` | ~11.5M |
| `title_ratings` | `title.ratings.tsv.gz` | ~1.5M |
| `title_akas` | `title.akas.tsv.gz` | ~48M |
| `title_crew` | `title.crew.tsv.gz` | ~10M |
| `title_episode` | `title.episode.tsv.gz` | ~8M |
| `title_principals` | `title.principals.tsv.gz` | ~45M |
| `name_basics` | `name.basics.tsv.gz` | ~13M |
| `import_meta` | internal | 2 rows |
| `imdb_parental` | scraped | grows organically |

### Access Patterns

- **14 SQLite connection points** in production code (12 in `main.py` via `aiosqlite`, 1 in `importer.py` via `sqlite3`, 1 in `charts.py` via `sqlite3`)
- **Import**: runs via `asyncio.to_thread()`, uses `BEGIN IMMEDIATE` / `COMMIT`, bulk `INSERT OR REPLACE` in 10K batches
- **Search**: dynamically constructed SQL with 18+ optional WHERE filters (EXISTS subqueries on `title_akas`, `title_principals`, `title_episode`)
- **Charts**: read-only, fetches all rows, computes Bayesian weighted ratings in Python
- **Parental cache**: per-item upsert, TTL 90 days

## Hosting Options

| Option | Cost | Notes |
|--------|------|-------|
| **Oracle Cloud Always Free** | $0 | 4 OCPU, 24 GB RAM VM + up to 200 GB block storage. Run Postgres in Docker on the VM or use their free Autonomous DB (20 GB). |
| **AWS RDS db.t4g.micro** | ~$15/mo | 1 vCPU, 1 GB RAM, 20 GB gp3. Enough if schemas are optimized. Burstable credits. |
| **AWS RDS db.t4g.small** | ~$30/mo | 2 vCPU, 4 GB RAM. Comfortable for full 20–40 GB dataset. |
| **Render Postgres** | $7–$15/mo | Simpler than RDS, no READ replicas or same HA guarantees. |
| **Self-hosted (same Docker stack)** | $0 | Run a Postgres container (like Fider already does). Adds disk pressure to the same server. |

**Recommendation**: Oracle Cloud always-free if the VM is already set up; AWS RDS db.t4g.micro for managed. The real cost difference is ~$0 vs ~$15/mo.

## SQL Dialect Changes

| SQLite | Postgres |
|--------|----------|
| `INSERT OR REPLACE INTO t VALUES (...)` | `INSERT INTO t VALUES (...) ON CONFLICT (pk) DO UPDATE SET ...` |
| `LIKE '%pattern%'` (case-insensitive for ASCII) | `ILIKE '%pattern%'` |
| `PRAGMA journal_mode=WAL` | `wal_level = logical` + `full_page_writes = on` |
| `PRAGMA synchronous=NORMAL` | `synchronous_commit = on` |
| `conn.executemany(sql, batch)` | `cur.executemany(sql, batch)` (same interface in psycopg) |
| `typeof()` / implicit type coercion | Strict typing — cast explicitly |
| `SELECT value FROM import_meta WHERE key='row_counts'` | Same syntax |
| `json.loads(row[0])` from TEXT column | `row[0]` is already a Python dict if using JSONB |
| `CREATE TABLE IF NOT EXISTS` | Same syntax |
| `INSERT INTO ... ON CONFLICT DO UPDATE` | Already used for parental cache — works as-is |
| `DELETE FROM table` | Same syntax (`TRUNCATE` faster for full-table deletes) |
| `executescript(SCHEMA_SQL)` | Works but fragile with semicolons — use individual statements |

## Connection Model

Current (aiosqlite):
```python
async with aiosqlite.connect(DB_PATH) as db:
    cursor = await db.execute("SELECT ...")
```

Postgres (asyncpg):
```python
async with pool.acquire() as conn:
    row = await conn.fetchrow("SELECT ...")
```

One module-level pool initialized in FastAPI's `lifespan()`:
```python
pool = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=2, max_size=10)
```

## Migration Strategy — Two Phase

### Phase 1: Dual-Mode

Add `DATABASE_URL` env var. When set, use Postgres (`asyncpg`). When unset, keep SQLite. This allows:

1. Deploy dual-mode code to staging
2. Set up the Postgres instance
3. Run one-time migration script (dump SQLite → pg)
4. Switch `DATABASE_URL` in production
5. Monitor, then remove SQLite code path

Files to create/modify:

| File | Change |
|------|--------|
| `imdb-service/pg.py` (new) | Connection pool init, schema DDL translated to Postgres |
| `imdb-service/main.py` | 14 connection sites → `pool.acquire()`; keep SQLite as fallback |
| `imdb-service/importer.py` | `import_to_postgres()` alongside `run_direct_import()`; use `COPY` |
| `imdb-service/charts.py` | `rebuild_charts_from_pg()` alongside `rebuild_all_charts()` |
| `imdb-service/requirements.txt` | Add `asyncpg` |
| `imdb-service/Dockerfile` | No change needed |
| `docker-compose.yml` | Add `DATABASE_URL` env var to imdb-service |

### Phase 2: Full Postgres

Remove all SQLite code paths once stable.

## Implementation Details

### Bulk Import (replaces `import_table`)

Postgres `COPY` is 5–10× faster than batched INSERT for large datasets. Use `StringIO` + `copy_from`:

```python
import io
import gzip

buffer = io.StringIO()
with gzip.open(gz_path, "rt", encoding="utf-8") as f:
    f.readline()  # skip header
    for line in f:
        buffer.write(line)
buffer.seek(0)

async with pool.acquire() as conn:
    await conn.copy_from(buffer, table, columns=columns, sep="\t", null=r"\N")
```

### Search Endpoint

Dynamic SQL builder needs minimal changes:
- `LIKE` → `ILIKE` (all genre/title patterns)
- `?` placeholders → `$1, $2, ...` (asyncpg uses `$N` syntax)

OR rewrite with the asyncpg `executemany` equivalent — the `SORT_COLUMN_MAP` allowlist already prevents injection.

### Charts

Same queries, just via asyncpg. The Bayesian computation stays in Python.

### Migration Script (`migrate_to_pg.py`)

```python
1. Open SQLite DB (read-only)
2. Connect to Postgres via DATABASE_URL
3. Create PG schema (DDL)
4. For each table:
   a. Stream rows from SQLite via fetchall / iteration
   b. COPY into postgres via StringIO + copy_from
5. Set import_meta (last_refresh, row_counts)
6. Verify row counts match
```

Estimated migration time for full dataset: 30–60 minutes.

## Risks

- **`title_akas` (48M rows) and `title_principals` (45M rows)** — COPY takes 5–10 min per table. The import runs in a background thread, so the API stays up during migration.
- **Search query performance** — Postgres handles complex WHERE clauses + EXISTS subqueries better than SQLite, but test with `EXPLAIN ANALYZE` on the worst-case path (all 18 filters active).
- **Memory on db.t4g.micro (1 GB)** — The Bayesian chart computation pulls 1.5M rating rows into the *app server's* memory, not the DB's. Same footprint as SQLite. Fine.
- **Genres `LIKE` pattern** — Comma-separated genres (`"Action,Comedy,Drama"`) queried via `g.genres ILIKE '%Comedy%'` is a full scan. Consider a `title_genres` junction table for proper indexing. Not required for migration but worth a separate issue.

## Estimated Effort

| Step | Complexity | Lines Changed |
|------|-----------|---------------|
| `pg.py` module | Low | ~60 |
| `main.py` query swap (14 sites) | Medium | ~200 |
| `importer.py` PG bulk import | High | ~150 |
| `charts.py` PG chart rebuild | Low | ~40 |
| Migration script | Medium | ~100 |
| Dockerfile + config | Low | ~10 |
| **Total** | | **~560 lines** |

## When to Do This

The WAL direct-import fix (2026-07-15) already solved the disk pressure. Postgres adds:
- $7–$15/mo hosting cost
- ~2 days dev + testing time
- Another dependency (SSL certs, pool tuning, version upgrades)
- Zero-downtime migration complexity

**Do this when one of these is true:**
1. You want managed backups / RDS snapshots
2. You're scaling to multiple app containers
3. Oracle Cloud's free tier is already set up and you want to exercise it

Otherwise, the WAL fix is sufficient and Postgres is nice-to-have, not urgent.
