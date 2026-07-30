# Trakt OAuth - Kometa

A static, fully client-side web page for authenticating with Trakt and obtaining access tokens for use with Kometa.

**No user-sensitive data touches the server.** The page is served as a static file; the Client ID/Secret, authorization code, and resulting tokens are held only in the user's browser and exchanged directly with Trakt's CORS-enabled OAuth endpoints.

## Features

- Clean, modern UI with Trakt branding
- Browser-only OAuth: token exchange is a `fetch()` straight to `api.trakt.tv`, never proxied through this server
- Single-use, ten-minute authorization state held in `sessionStorage` (anti-CSRF)
- Privacy headers: `Cache-Control: no-store`, `Referrer-Policy: no-referrer`, and a restrictive Content-Security-Policy
- Automatic configuration generation for Kometa
- Copy-to-clipboard functionality

## How It Works

1. User enters their own Trakt `client_id` and `client_secret` (kept in browser memory only).
2. The page generates a random `state`, stashes credentials + state in `sessionStorage`, and redirects to Trakt's authorize URL.
3. Trakt redirects back to `/trakt-oauth/callback?code=...&state=...` — the **same Redirect uri as the previous server-side flow**, so existing Trakt app registrations keep working.
4. The page validates `state` (single-use, 10-minute TTL), then POSTs the code directly to `https://api.trakt.tv/oauth/token`.
5. The Kometa `config.yml` snippet is rendered client-side.

## Files

- `static/index.html` — the entire application (HTML + CSS + JS, no dependencies)
- `trakt_oauth/`, `templates/`, `Dockerfile` — legacy Flask implementation, retained as a fallback but no longer routed

## Deployment

The page is served by the Caddy container, which mounts `./trakt-oauth/static` at `/var/www/html/trakt-oauth` (see `docker-compose.yml`). The Caddyfile serves `index.html` for both `/trakt-oauth/` and `/trakt-oauth/callback` and sets the security headers:

```caddy
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

No container, Python environment, or environment variables are required for the static page.

## Legacy Flask App (fallback)

The previous Flask service remains in this directory and in `docker-compose.yml` but receives no traffic once Caddy serves the static page. To roll back, restore the old `handle /trakt-oauth*` reverse-proxy block in the Caddyfile.

Environment variables used by the legacy app only:

- `PORT` - Port to run on (default: 8080)
- `HOST` - Host to bind to (default: 127.0.0.1)
- `DEBUG` - Enable debug mode (default: False)
- `SECRET_KEY` - Flask secret key (default: dev-key-change-in-production)
- `TRAKT_REDIRECT_URI` - Exact callback URI registered in the Trakt application (default: `http://localhost:8080/callback`)
