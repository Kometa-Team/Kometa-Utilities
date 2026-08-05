# MyAnimeList OAuth - Kometa

A web page for authenticating with MyAnimeList and obtaining access tokens for use with Kometa, with two modes:

- **Use Kometa App** — the shared Kometa MyAnimeList application. The browser asks the Flask backend for an authorization URL and later to exchange the code. MAL's PKCE flow needs no `client_secret`, so the official secret never reaches the browser at all.
- **Bring Your Own** — the user's own MAL app. Credentials are held only in the browser; the token exchange transits a same-origin Caddy passthrough.

Unlike Trakt, MyAnimeList's OAuth endpoints send **no CORS headers**, so the token exchange cannot be fetched cross-origin. In BYO mode the page POSTs the exchange to a same-origin path (`/mal-oauth/api/token`), which Caddy forwards untouched to `https://myanimelist.net/v1/oauth2/token`. Nothing is stored server-side.

## Features

- Clean, modern UI with MyAnimeList branding
- Official flow routes `/mal-oauth/api/official/*` through the Flask backend (via Caddy); the PKCE `code_verifier` and single-use `state` are generated and held server-side
- BYO flow: browser-only OAuth with PKCE (`plain` challenge, the only method MAL supports); the `client_secret` is optional thanks to PKCE
- Single-use, ten-minute authorization state (anti-CSRF)
- Privacy headers: `Cache-Control: no-store`, `Referrer-Policy: no-referrer`, and a restrictive Content-Security-Policy (`connect-src 'self'`)
- Automatic configuration generation for Kometa (official config omits `client_secret`)
- Copy-to-clipboard functionality

## How It Works

### Use Kometa App (official)

1. The page POSTs to `/mal-oauth/api/official/start`; the backend generates a single-use `state` and PKCE `code_verifier`, stores them with the official `client_id` (10-minute TTL), and returns the MAL authorization URL.
2. The page redirects to `https://myanimelist.net/v1/oauth2/authorize` (`code_challenge_method=plain`).
3. MAL redirects back to `/mal-oauth/callback?code=...&state=...`.
4. The page POSTs `{code, state}` to `/mal-oauth/api/official/exchange`; the backend validates the single-use state and exchanges the code server-to-server, sending no `client_secret`.
5. The Kometa `config.yml` snippet (with `client_id` but no `client_secret`) is rendered client-side.

### Bring Your Own

1. User enters their own MAL `client_id` (kept in browser memory only); the `client_secret` is optional.
2. The page generates a random `state` and PKCE `code_verifier`, stashes them in `sessionStorage`, and redirects to the MAL authorize URL.
3. MAL redirects back to `/mal-oauth/callback?code=...&state=...`.
4. The page validates `state` (single-use, 10-minute TTL), then POSTs the code to the same-origin `/mal-oauth/api/token`, which Caddy proxies to MAL's token endpoint.
5. The Kometa `config.yml` snippet is rendered client-side.

## Files

- `static/index.html` — the entire application (HTML + CSS + JS, no dependencies)
- `static/logo.svg`, `static/myanimelist-logo.svg` — MAL logo (landing page and legacy app)
- `mal_oauth/`, `templates/`, `Dockerfile` — Flask backend (serves the official `/api/official/*` endpoints)
- `.env` — gitignored; holds the official `CLIENT_ID`
- `javascript_impl.js`, `javascript_impl.html` — reference implementations of the MAL OAuth flow (Express and pure-browser demo)

## Deployment

The page is served by the Caddy container, which mounts `./mal-oauth/static` at `/var/www/html/mal-oauth` (see `docker-compose.yml`). The Flask backend (also in `docker-compose.yml`, image `ghcr.io/kometa-team/mal-oauth`) receives only `/mal-oauth/api/official/*`. The Caddyfile:

```caddy
# Official "Use Kometa App" backend routes
handle /mal-oauth/api/official/* {
    uri strip_prefix /mal-oauth
    reverse_proxy mal-oauth:5000
}

# BYO token exchange passthrough to MyAnimeList
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

The official flow requires the backend container and the `mal-oauth/.env` file:

```env
CLIENT_ID=<official-client-id>
CLIENT_SECRET=
MAL_REDIRECT_URI=https://utilities.kometa.wiki/mal-oauth/callback
```

Only `CLIENT_ID` is needed; leave `CLIENT_ID` blank to disable the official flow (the page's "Use Kometa App" tab will report the app is unconfigured and users can fall back to "Bring Your Own").

## Backend Environment Variables

- `PORT` - Port to run on (default: 8080)
- `HOST` - Host to bind to (default: 127.0.0.1)
- `DEBUG` - Enable debug mode (default: False)
- `SECRET_KEY` - Flask secret key (default: dev-key-change-in-production)
- `ROOT_PATH` - URL prefix when served behind a proxy (default: empty)
- `CLIENT_ID` - Official Kometa MAL app client id (empty disables the official flow)
- `CLIENT_SECRET` - Optional official secret; not required for MAL's PKCE flow
- `MAL_REDIRECT_URI` - Exact callback URI registered in the MAL application (default: `https://utilities.kometa.wiki/mal-oauth/callback`)
