# Path-Based Routing - Configuration Summary

## ✅ Your Current Working Setup

Based on your successful deployment at `https://utilities.kometa.wiki/anidb-service/`:

### Working Configuration

**Caddyfile:**
```caddy
utilities.kometa.wiki {
    handle_errors {
        @maintenance expression {err.status_code} in [502, 503, 504]
        handle @maintenance {
            rewrite * /maintenance.html?path={path}&host={host}
            file_server {
                root /var/www/html
            }
        }
    }

    handle /anidb-service* {
        uri strip_prefix /anidb-service
        reverse_proxy anidb-mirror:8000
    }
}
```

**.env:**
```bash
ROOT_PATH=/anidb-service
```

### ✅ Working Endpoints

- `https://utilities.kometa.wiki/anidb-service/` ✓
- `https://utilities.kometa.wiki/anidb-service/stats` ✓
- `https://utilities.kometa.wiki/anidb-service/anime/1` ✓
- `https://utilities.kometa.wiki/anidb-service/search/tags?tags=action,comedy&min_weight=300` ✓
- `https://utilities.kometa.wiki/anidb-service/tags` ✓

### ⚠️ Known Issue: API Documentation

The `/docs` and `/redoc` endpoints have a known issue with path-based routing when using `uri strip_prefix`:

**Problem:** When Caddy strips `/anidb-service` prefix before proxying:
- Request: `https://utilities.kometa.wiki/anidb-service/docs`
- Caddy sends to backend: `/docs`
- FastAPI serves `/docs` page correctly
- BUT: The page tries to load OpenAPI spec from `/anidb-service/openapi.json`
- Since the prefix was stripped, FastAPI serves it at `/openapi.json`
- Result: "No API definition provided" error

**Workaround Options:**

#### Option 1: Access API Spec Directly (Recommended)
Use tools like curl or Postman to access the API directly:
```bash
# Get OpenAPI spec
curl https://utilities.kometa.wiki/anidb-service/openapi.json

# The spec is accessible, just not through the web UI
```

#### Option 2: Use Subdomain Instead
If API docs are important, use subdomain routing instead:

**Caddyfile:**
```caddy
anidb-service.utilities.kometa.wiki {
    handle_errors {
        @maintenance expression {err.status_code} in [502, 503, 504]
        handle @maintenance {
            rewrite * /maintenance.html?path={path}&host={host}
            file_server {
                root /var/www/html
            }
        }
    }

    reverse_proxy anidb-mirror:8000
}
```

**.env:**
```bash
ROOT_PATH=
```

Then `/docs` and `/redoc` will work at:
- `https://anidb-service.utilities.kometa.wiki/docs`
- `https://anidb-service.utilities.kometa.wiki/redoc`

#### Option 3: Alternative Documentation

Since all API endpoints work correctly, you can:
1. Use the OpenAPI spec with external tools (Postman, Insomnia, etc.)
2. Document endpoints in your own wiki/docs
3. Access the raw spec: `https://utilities.kometa.wiki/anidb-service/openapi.json`

## 📊 API Endpoint Reference

Since the interactive docs don't work in path-based mode, here's a quick reference:

### Main Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Service info |
| `/stats` | GET | Service statistics |
| `/anime/{aid}` | GET | Get anime by ID |
| `/search/tags` | GET | Search by tags |
| `/search/sequels` | GET | Find sequels |
| `/search/prequels` | GET | Find prequels |
| `/tags` | GET | Browse all tags |
| `/openapi.json` | GET | OpenAPI specification |

### Example Usage

**Get anime details:**
```bash
curl https://utilities.kometa.wiki/anidb-service/anime/1
```

**Search by tags:**
```bash
curl "https://utilities.kometa.wiki/anidb-service/search/tags?tags=action,comedy&min_weight=300"
```

**Get service stats:**
```bash
curl https://utilities.kometa.wiki/anidb-service/stats
```

## 🎯 Summary

Your deployment is **working correctly** for all actual API endpoints. The only limitation is the interactive web-based documentation (`/docs` and `/redoc`), which is a known technical limitation of combining:
- FastAPI's `root_path` feature
- Caddy's `uri strip_prefix`
- Swagger UI's asset loading

All API functionality is 100% operational - just use the API directly rather than through the web UI documentation.

## 🔧 Technical Explanation

When using path-based routing with prefix stripping:

1. **External Request:** `https://utilities.kometa.wiki/anidb-service/stats`
2. **Caddy Receives:** `/anidb-service/stats`
3. **Caddy Strips Prefix:** `/stats`
4. **FastAPI Receives:** `/stats`
5. **FastAPI Processes:** Route `/stats` with `root_path=/anidb-service`
6. **Response:** Returns data (works perfectly!)

For `/docs`:
1. **External Request:** `https://utilities.kometa.wiki/anidb-service/docs`
2. **Caddy Receives:** `/anidb-service/docs`
3. **Caddy Strips Prefix:** `/docs`
4. **FastAPI Receives:** `/docs`
5. **FastAPI Returns:** HTML page for Swagger UI
6. **Swagger UI Tries:** Load `/anidb-service/openapi.json` (because of `root_path`)
7. **But Caddy Strips:** `/anidb-service/openapi.json` → `/openapi.json`
8. **Result:** Mismatch - Swagger looks for prefixed path, but it was stripped

This is why the API works but docs don't in this configuration.
