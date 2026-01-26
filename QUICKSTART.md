# 🚀 Quick Start Guide

Get the AniDB Service running in under 5 minutes!

## Prerequisites

- Docker & Docker Compose installed
- Text editor (nano, vim, or VS Code)

## Steps

### 1. Configure Environment

```bash
# Copy the template
cp .env.example .env

# Edit with your settings
nano .env
```

**Minimum required changes:**
- Change `API_PASS` to a secure password

### 2. Update Domain

Copy and edit `Caddyfile`:

```bash
# Copy example configuration
cp Caddyfile.example Caddyfile

# Edit with your domain
nano Caddyfile
# Change: yourdomain.com
# To: your-actual-domain.com
```

**For path-based routing** (yourdomain.com/anidb-service):
- Keep the handle /anidb-service* section
- Set `ROOT_PATH=/anidb-service` in .env

**For subdomain routing** (anidb-service.yourdomain.com):
- Uncomment the subdomain section
- Comment out the path-based section
- Leave `ROOT_PATH` empty in .env

### 3. Start Services

```bash
docker compose up -d --build
```

### 4. Verify

```bash
# Check status (no auth required)
curl http://localhost:8000/stats

# Test authentication (replace with your password)
curl -u kometa_admin:your_password http://localhost:8000/anime/1
```

## Expected Response

First request for a new anime will return:
```json
{
  "detail": "AID 1 queued for fetching. Check back in a few moments."
}
```

After ~10 seconds, retry and you'll get XML:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<anime id="1">
  ...
</anime>
```

## What's Happening?

1. **First Request**: AniDB worker queues the anime ID
2. **Background**: Worker fetches from AniDB (respecting 4-second throttle)
3. **Cache**: XML saved to `./data/` and indexed to `database.db`
4. **Subsequent Requests**: Served instantly from cache

## Common Commands

```bash
# View logs
docker compose logs -f

# Restart
docker compose restart

# Stop
docker compose down

# Rebuild after code changes
docker compose up -d --build

# Check database
docker compose exec anidb-mirror sqlite3 /app/database.db "SELECT COUNT(*) FROM anime;"
```

## Troubleshooting

**"Connection refused"**: Services still starting, wait 10 seconds

**"401 Unauthorized"**: Check credentials in `.env`

**"Daily limit reached"**: Wait 24 hours or increase `DAILY_LIMIT` in `.env`

## Next Steps

- Read [SETUP.md](SETUP.md) for production deployment
- Review [README.md](README.md) for architecture details
- Check [CHANGELOG.md](CHANGELOG.md) for recent updates

## Support

Check logs first:
```bash
docker compose logs -f anidb-mirror
```

Most issues are related to:
- Missing/incorrect `.env` file
- Domain not pointing to server
- AWS credentials not set
- AniDB API temporary ban
