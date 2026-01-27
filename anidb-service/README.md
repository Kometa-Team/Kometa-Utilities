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
- Tag-based search
- Mature content filtering
