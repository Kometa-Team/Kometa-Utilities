# MyAnimeList OAuth - Kometa

A static, fully client-side web page for authenticating with MyAnimeList and obtaining access tokens for use with Kometa.

**Credentials and tokens are held only in the user's browser.** The page is served as a static file; the Client ID/Secret, authorization code, and resulting tokens live in browser memory (with a brief, single-use stay in `sessionStorage` across the MAL redirect).

Unlike Trakt, MyAnimeList's OAuth endpoints send **no CORS headers**, so the token exchange cannot be fetched cross-origin. The page instead POSTs the exchange to a same-origin path (`/mal-oauth/api/token`), which Caddy forwards untouched to `https://myanimelist.net/v1/oauth2/token`. Nothing is stored server-side.

## Features

- Clean, modern UI with MyAnimeList branding
- Browser-only OAuth with PKCE (`plain` challenge, the only method MAL supports)
- Single-use, ten-minute authorization state held in `sessionStorage` (anti-CSRF)
- Privacy headers: `Cache-Control: no-store`, `Referrer-Policy: no-referrer`, and a restrictive Content-Security-Policy (`connect-src 'self'`)
- Automatic configuration generation for Kometa
- Copy-to-clipboard functionality

## How It Works

1. User enters their own MAL `client_id` and `client_secret` (kept in browser memory only).
2. The page generates a random `state` and PKCE `code_verifier`, stashes them in `sessionStorage`, and redirects to `https://myanimelist.net/v1/oauth2/authorize` (`code_challenge_method=plain`).
3. MAL redirects back to `/mal-oauth/callback?code=...&state=...`.
4. The page validates `state` (single-use, 10-minute TTL), then POSTs the code to the same-origin `/mal-oauth/api/token`, which Caddy proxies to MAL's token endpoint.
5. The Kometa `config.yml` snippet is rendered client-side.

## Files

- `static/index.html` — the entire application (HTML + CSS + JS, no dependencies)
- `static/logo.svg`, `static/myanimelist-logo.svg` — MAL logo (landing page and legacy app)
- `mal_oauth/`, `templates/`, `Dockerfile` — legacy Flask implementation, retained as a fallback but no longer routed
- `javascript_impl.js`, `javascript_impl.html` — reference implementations of the MAL OAuth flow (Express and pure-browser demo)

## Deployment

The page is served by the Caddy container, which mounts `./mal-oauth/static` at `/var/www/html/mal-oauth` (see `docker-compose.yml`). The Caddyfile serves `index.html` for both `/mal-oauth/` and `/mal-oauth/callback`, proxies the token exchange, and sets the security headers:

```caddy
handle /mal-oauth/api/token* {
    rewrite * /v1/oauth2/token
    reverse_proxy https://myanimelist.net {
        header_up Host myanimelist.net
    }
}

handle /mal-oauth* {
    root * /var/www/html
    @malCallback path /mal-oauth/callback
    rewrite @malCallback /mal-oauth/index.html
    header Cache-Control "no-store"
    header Referrer-Policy "no-referrer"
    header X-Content-Type-Options "nosniff"
    file_server
}
```

Register `https://<your-domain>/mal-oauth/callback` as the **App Redirect URL** in your MyAnimeList application (the page derives and displays the exact value from `window.location`). This differs from the `localhost` URL used by the legacy flow — existing MAL apps must update the registered redirect URL.

No container, Python environment, or environment variables are required for the static page.

## Legacy Flask App (fallback)

The previous Flask service remains in this directory and in `docker-compose.yml` but receives no traffic once Caddy serves the static page. To roll back, restore the old `handle /mal-oauth*` reverse-proxy block in the Caddyfile and remove the `/mal-oauth/api/token` proxy block.

Environment variables used by the legacy app only:

- `PORT` - Port to run on (default: 8080)
- `HOST` - Host to bind to (default: 127.0.0.1)
- `DEBUG` - Enable debug mode (default: False)
- `SECRET_KEY` - Flask secret key (default: dev-key-change-in-production)
