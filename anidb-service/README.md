# AniDB Mirror Service

FastAPI-based caching service for AniDB anime metadata with rate limiting and background updates.

## Setup

1. Copy `.env.example` to `.env`:
   ```bash
   cp .env.example .env
   ```

2. Configure your environment variables in `.env`

3. Deploy with Docker Compose (from root directory):
   ```bash
   docker compose up -d anidb-mirror
   ```

## Access

- Service URL: `https://yourdomain.com/anidb-service`
- API Documentation: `https://yourdomain.com/anidb-service/docs`

## Development

Install dependencies:
```bash
pip install -r requirements.txt
```

Run tests:
```bash
pytest
```

Run locally:
```bash
uvicorn main:app --reload
```

## Features

- Caches AniDB anime metadata locally
- Rate limiting to respect AniDB API limits
- Background worker for async updates
- Tag-based search with mature content filtering
- Per-request mature content filtering

## API Endpoints

### GET /anime/{aid}
Fetch anime metadata by AniDB ID.

**Parameters:**
- `aid` (required): AniDB anime ID
- `mature` (optional, default: `false`): Include mature/18+ content

**Examples:**
```bash
# Get anime with adult content filtered out (default)
curl "http://localhost/anime/123"

# Get anime with adult content included
curl "http://localhost/anime/123?mature=true"
```

**Response Headers:**
- `X-Cache`: `HIT`, `STALE`, or not present (queued)
- `X-Mature-Filter`: `enabled` or `disabled`
- `X-Age-Days`: Cache age in days
- `X-Refresh-After-Days`: How long this entry stays fresh (see Refresh Tiers)

### GET /search/tags
Search for anime by tags.

**Parameters:**
- `tags` (required): Comma-separated list of tags
- `min_weight` (optional, default: 200): Minimum tag weight
- `mature` (optional, default: `false`): Include mature/18+ anime in results

**Examples:**
```bash
# Search excluding adult anime (default)
curl "http://localhost/search/tags?tags=action,comedy"

# Search including adult anime in results
curl "http://localhost/search/tags?tags=action,comedy&mature=true"

# With minimum weight filter
curl "http://localhost/search/tags?tags=action&min_weight=300&mature=false"
```

**Response:**
```json
{
  "query": ["action", "comedy"],
  "min_weight": 200,
  "mature": false,
  "results": [
    {"aid": 123, "tag_matches": 2},
    {"aid": 456, "tag_matches": 2}
  ]
}
```

**Mature Filtering:**
When `mature=false`, anime with the following tags are excluded:
- "18 restricted"
- "hentai"
- "pornography"
- "adult"

### GET /stats
Get service statistics.

**Response:**
```json
{
  "status": "online",
  "cached_anime": 1500,
  "api_calls_last_24h": 45,
  "queue_size": 2,
  "queued_new": 1,
  "queued_refresh": 1,
  "daily_limit": 200
}
```

- `queued_new`: queued AIDs with no cached copy (the new queue). The worker always drains this first.
- `queued_refresh`: queued AIDs with a stale cached copy (the refresh queue). The worker only takes from it when the new queue is empty.

The queue and any 429 back-off are stored in the database, so a restart does not lose the backlog.

### GET /tags
List all known tags with usage statistics (HTML page).

## Abuse Protection

The service only caches what is requested, so uncached lookups are limited. Cache hits and
refreshes of cached entries are never limited.

| Check | Env var | Default | Response |
|---|---|---|---|
| AID above the real range | `MAX_AID` | 50000 | 404 |
| New AIDs queued per IP per window | `NEW_LOOKUPS_PER_IP` / `NEW_LOOKUP_WINDOW_SECONDS` | 50 / 3600 | 429 + `Retry-After` |
| New AIDs queued per IP per rolling 24h | `NEW_LOOKUPS_PER_IP_DAILY` | 100 | 429 + `Retry-After` |
| New queue already full | `MAX_QUEUED_NEW` | 5000 | 503 + `Retry-After` |

- A request for an AID that is already queued doesn't count against the caller.
- Callers are identified by the last `X-Forwarded-For` value (the one Caddy adds), falling back
  to the socket address. IPv6 callers are grouped by /64. See `CLIENT_IP_HEADER` in `.env.example`
  before putting a CDN in front of Caddy.
- `/stats` has `rejected_new_lookups` (`out_of_range`, `rate_limited` for the hourly limit, `daily_limited`, `queue_full`), counted since
  the last restart. The first rejection for an IP in each window is logged.
- Limits are in memory and reset on restart. Setting any value to `0` turns that check off.

## Refresh Tiers

How long a cached entry stays fresh depends on its AniDB `startdate`/`enddate`, so the daily
API budget goes to entries that actually change. Past the threshold, the stale copy is served
and the AID is added to the refresh queue, which the worker only works on once the new queue is empty.

| Entry | Env var | Default |
|---|---|---|
| Airing, open-ended, upcoming, or end date in the future | `REFRESH_DAYS_ACTIVE` | 14 |
| No start date | `REFRESH_DAYS_UNKNOWN` | 30 |
| Ended under 1 year ago | `REFRESH_DAYS_ENDED_UNDER_1Y` | 30 |
| Ended 1-3 years ago | `REFRESH_DAYS_ENDED_1_3Y` | 90 |
| Ended 3-10 years ago | `REFRESH_DAYS_ENDED_3_10Y` | 180 |
| Ended 10+ years ago | `REFRESH_DAYS_ENDED_10Y_PLUS` | 365 |

`UPDATE_THRESHOLD_DAYS` is only the fallback for XML that can't be parsed.

## Mature Content Filtering

The service supports two levels of mature content control:

1. **API Access** (via AniDB credentials):
   - Set `ANIDB_USERNAME` and `ANIDB_PASSWORD` in `.env`
   - Required to fetch mature anime from AniDB
   - Without credentials, adult anime cannot be retrieved

2. **Client-Side Filtering** (per-request, opt-in):
   - `/anime/{aid}?mature=true` - Include adult content (filtered by default)
   - `/search/tags?mature=true` - Include adult anime (excluded by default)
   - Default behavior is family-friendly; users must explicitly request mature content
