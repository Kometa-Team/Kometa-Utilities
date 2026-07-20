# Kometa Utilities

Kometa Utilities is a collection of backend services designed to support the Kometa ecosystem. It provides secure OAuth authentication flows for third-party platforms (Plex, Trakt, MyAnimeList, and SIMKL) alongside robust API proxying and metadata caching services (IMDb, AniDB, and SIMKL). By handling authentication and rate-limited data scraping centrally, these utilities ensure fast, reliable, and compliant data access for Kometa users.

## what's here?

A suite of support services for Kometa, including:
* OAuth Proxies: Secure authentication handling for Plex, Trakt, MyAnimeList, and SIMKL.
* Metadata & Chart Caching: Automated background scraping and API caching for IMDb (including parental guides and daily charts), AniDB, and SIMKL.
* Community Infrastructure: Integrated hosting for Kometa's analytics, feature requests, translation management, and file sharing.

## Docker Workflow

This repository publishes Docker images for its locally maintained utility services:

- `ghcr.io/kometa-team/anidb-mirror`
- `ghcr.io/kometa-team/plex-oauth`
- `ghcr.io/kometa-team/trakt-oauth`
- `ghcr.io/kometa-team/mal-oauth`
- `ghcr.io/kometa-team/simkl-oauth`
- `ghcr.io/kometa-team/simkl-service`
- `ghcr.io/kometa-team/imdb-service`

The base `docker-compose.yml` uses those published GHCR images.

For local development, `docker-compose.override.yml` is automatically picked up by Docker Compose and switches those same services back to local `build:` contexts. In practice that means:

- server/production usage pulls published images by default
- local development builds from source by default

If you want to force a local rebuild, use:

```bash
docker compose up -d --build
```

For `imdb-service`, the Playwright/browser layer now lives in a separate base image so code-only updates stay small. To rebuild both the IMDb base image and the app image locally in one step, use:

```bash
./imdb-service/build-local.sh
```

Optional overrides:

```bash
BASE_IMAGE=ghcr.io/kometa-team/imdb-service-base:dev \
APP_IMAGE=ghcr.io/kometa-team/imdb-service:dev \
./imdb-service/build-local.sh
```

## GitHub Actions

The repository includes two Docker-related GitHub Actions workflows:

- **Pull requests to `main`**: build the utility images for validation only
- **Pushes to `main`**: build and publish the utility images to GHCR

Relevant workflow files:

- `.github/workflows/docker-pr-build.yml`
- `.github/workflows/docker-build.yml`
- `.github/workflows/deploy.yml`

This lets contributors verify Docker changes in PRs while keeping image publishing limited to the `main` branch.

## Deployment Workflow

The repository also includes a deployment workflow that connects to the remote server over SSH after the `Docker Builds` workflow completes successfully on `main`.

The workflow:

- pulls the latest Git branch on the server
- runs `docker compose pull`
- runs `docker compose up -d`
- prunes dangling Docker images

Required GitHub Actions secrets:

- `DEPLOY_HOST`
- `DEPLOY_USER`
- `DEPLOY_SSH_KEY`
- `DEPLOY_PROJECT_DIR`

Optional secrets:

- `DEPLOY_PORT` (defaults to `22`)
- `DEPLOY_BRANCH` (defaults to `main`)

## IMDb Parental Proxying

The IMDb parental guide fetch path supports optional proxy rotation so repeated browser-backed parental requests do not all originate from the same IP.

Supported environment variables for `imdb-service`:

- `PARENTAL_PROXY_ENABLED`
- `PARENTAL_PROXY_URLS`
- `PARENTAL_PROXY_RETRY_COUNT`
- `PARENTAL_PROXY_BAN_TTL_MINUTES`

Example:

```env
PARENTAL_PROXY_ENABLED=true
PARENTAL_PROXY_URLS=http://user:pass@proxy1.example.com:8080,http://user:pass@proxy2.example.com:8080
PARENTAL_PROXY_RETRY_COUNT=2
PARENTAL_PROXY_BAN_TTL_MINUTES=30
```

Notes:

- proxy rotation is used only for IMDb parental guide fetches
- failed proxies are temporarily cooled down in-process before reuse
- cached parental guide data is still preferred, so proxy usage should stay low in normal operation
- for docker compose deployments, put these values in the server-side `.env` file rather than committing credentials into `docker-compose.yml`

Example server-side `.env` values:

```env
PARENTAL_PROXY_ENABLED=true
PARENTAL_PROXY_URLS=http://username:password@proxy-host:port
PARENTAL_PROXY_RETRY_COUNT=1
PARENTAL_PROXY_BAN_TTL_MINUTES=30
```
