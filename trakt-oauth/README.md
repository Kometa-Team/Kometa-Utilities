# Trakt OAuth - Kometa

A web page for authenticating with Trakt and obtaining access tokens for use with Kometa, with two modes:

- **Use Kometa App** — the shared Kometa Trakt application. The browser asks the Flask backend for an authorization URL and later to exchange the code; the official `client_id`/`client_secret` live only in the container's environment and never appear in served page source.
- **Bring Your Own** — the user's own Trakt app. Credentials, authorization code, and tokens are held only in the browser and exchanged directly with Trakt's CORS-enabled OAuth endpoints; nothing sensitive touches the server.

## Features

- Clean, modern UI with Trakt branding
- Official flow routes `/api/official/*` through the Flask backend (via Caddy), keeping the server-held secret out of the browser until the final config is produced
- BYO flow: token exchange is a `fetch()` straight to `api.trakt.tv`, never proxied through this server
- Single-use, ten-minute authorization state held in `sessionStorage` (anti-CSRF)
- Privacy headers: `Cache-Control: no-store`, `Referrer-Policy: no-referrer`, and a restrictive Content-Security-Policy
- Automatic configuration generation for Kometa
- Copy-to-clipboard functionality

## How It Works

### Use Kometa App (official)

1. The page POSTs to `/trakt-oauth/api/official/start`; the backend generates a single-use `state`, stores it with the official credentials (10-minute TTL), and returns the Trakt authorization URL.
2. The page redirects to Trakt.
3. Trakt redirects back to `/trakt-oauth/callback?code=...&state=...` — the **same Redirect uri as the previous server-side flow**, so existing Trakt app registrations keep working.
4. The page POSTs `{code, state}` to `/trakt-oauth/api/official/exchange`; the backend validates the single-use state and exchanges the code server-to-server.
5. Trakt needs the `client_secret` to refresh tokens, so the official `config.yml` snippet (including the server-held secret) is returned to the browser only after a successful exchange.

### Bring Your Own

1. The user enters their own Trakt `client_id`/`client_secret` (kept in browser memory only).
2. The page generates a random `state`, stashes credentials + state in `sessionStorage`, and redirects to Trakt's authorize URL.
3. Trakt redirects back to `/trakt-oauth/callback?code=...&state=...`.
4. The page validates `state` (single-use, 10-minute TTL), then POSTs the code directly to `https://api.trakt.tv/oauth/token`.
5. The Kometa `config.yml` snippet is rendered client-side.

## Files

- `static/index.html` — the entire application (HTML + CSS + JS, no dependencies)
- `trakt_oauth/`, `templates/`, `Dockerfile` — Flask backend (serves the static page via a mounted volume fallback and the official `/api/official/*` endpoints)
- `.env` — gitignored; holds the official `CLIENT_ID`/`CLIENT_SECRET`

## Deployment

The page is served by the Caddy container, which mounts `./trakt-oauth/static` at `/var/www/html/trakt-oauth` (see `docker-compose.yml`). The Flask backend (also in `docker-compose.yml`, image `ghcr.io/kometa-team/trakt-oauth`) receives only `/trakt-oauth/api/*`. The Caddyfile:

```caddy
# Official "Use Kometa App" backend routes
handle /trakt-oauth/api/* {
    uri strip_prefix /trakt-oauth
    reverse_proxy trakt-oauth:5000
}

handle /trakt-oauth* {
    root * /var/www/html
    @traktCallback path /trakt-oauth/callback
    rewrite @traktCallback /trakt-oauth/index.html
    header Cache-Control "no-store"
    header Referrer-Policy "no-referrer"
    header X-Content-Type-Options "nosniff"
    file_server
}
```

Register `https://<your-domain>/trakt-oauth/callback` as the Redirect uri in your Trakt application (the page derives and displays the exact value from `window.location`).

The official flow requires the backend container and the `trakt-oauth/.env` file:

```env
CLIENT_ID=<official-client-id>
CLIENT_SECRET=<official-client-secret>
```

Leave both blank to disable the official flow (the page's "Use Kometa App" tab will report the app is unconfigured and users can fall back to "Bring Your Own").

## Backend Environment Variables

- `PORT` - Port to run on (default: 8080)
- `HOST` - Host to bind to (default: 127.0.0.1)
- `DEBUG` - Enable debug mode (default: False)
- `SECRET_KEY` - Flask secret key (default: dev-key-change-in-production)
- `TRAKT_REDIRECT_URI` - Exact callback URI registered in the Trakt application (default: `http://localhost:8080/callback`)
- `CLIENT_ID` / `CLIENT_SECRET` - Official Kometa Trakt app credentials (empty disables the official flow)
