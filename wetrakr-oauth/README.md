# WeTrakr OAuth - Kometa (Static Device-Code Flow)

A fully client-side WeTrakr auth page using WeTrakr's OAuth 2.0 device-code flow.
The browser never talks to `api.wetrakr.com` directly — WeTrakr's `/oauth/device/*` endpoints send
no CORS headers (confirmed via a live OPTIONS preflight during Phase 0, 2026-09-28; no
`Access-Control-Allow-Origin` in the response), so a direct `fetch()` from the browser would fail.
The page instead POSTs to a same-origin path that Caddy forwards to WeTrakr untouched — see the
Caddyfile block below. No `client_secret` is used anywhere in this flow, so nothing sensitive ever
touches this server either way — same shape as FlickList's page in this repo, just with WeTrakr's
own headers and status-code-driven poll handling.

## How It Works

1. User clicks "Connect with WeTrakr"
2. Page requests a code via `POST /wetrakr-oauth/api/device/code` (proxied to
   `POST https://api.wetrakr.com/oauth/device/code` with `{"client_id": ...}`)
3. User opens the WeTrakr link shown (`https://wetrakr.com/activate?code=...`) and enters the code
4. Page polls `POST /wetrakr-oauth/api/device/token` (proxied to
   `POST https://api.wetrakr.com/oauth/device/token` with `{"code": ..., "client_id": ...}`) until
   WeTrakr returns a token pair
5. Configuration is displayed for copying into Kometa's `config.yml`

Every request to WeTrakr — including the device-code and token endpoints — needs the
`wetrakr-api-key` (the client_id) and `wetrakr-api-version: 1` headers. The page sets these directly
on its `fetch()` calls; Caddy's same-origin proxy forwards whatever headers the browser sends, so no
special proxy configuration is needed for them beyond the passthrough itself.

WeTrakr's device-flow polling is driven by HTTP status code, not by an `error` string in the body
(FlickList's page checks `error`; this one switches on `response.status`): 200 is success, 400 with
`error: "authorization_pending"` means keep waiting, 404/410/418 mean restart or stop, 409 means a
token pair was already issued for that code, and 429 means slow down but keep polling. See
`WETRAKR-API-CONTRACT.md` §4 for the full table this mirrors.

Unlike FlickList's permanent key, WeTrakr issues an access token (7 days) and a refresh token (180
days, rotating on every refresh). Kometa refreshes both automatically and rewrites `config.yml`; if
Kometa's config has `read_only` set, it can't do that, and the user needs to return to this page
every 7 days instead. The page says so. Requesting a new device code is rate-limited to 5 per 15
minutes per IP; the page surfaces a clear message on a 429 rather than a generic error.

## Configuration

`WETRAKR_CLIENT_ID` is a public identifier (no `client_secret` is involved anywhere in this flow, and
it doubles as the `wetrakr-api-key` header value), so — matching FlickList's own precedent in this
repo rather than SIMKL's env-var pattern — it's a committed constant directly in `static/index.html`,
not read from `CLIENT_IDS` or an env var.

**Open item:** the constant currently holds `cf00ea6da77e7c20528ca2a8b6885271`, the app registered
2026-09-27. Which WeTrakr account owns it long-term, and whether a separate production key is needed,
are both still open (see the project's WeTrakr integration plan, decision Q1) — this is the id Kometa
currently has, not necessarily the final one.

## Deployment

The page is served as static content by Caddy, with a same-origin passthrough for the two device
endpoints (WeTrakr's own CORS gap is the reason the passthrough exists — see FlickList's identical
passthrough for the established precedent in this repo):

```caddy
handle /wetrakr-oauth/api/device/code* {
    rewrite * /oauth/device/code
    reverse_proxy https://api.wetrakr.com {
        header_up Host api.wetrakr.com
    }
}

handle /wetrakr-oauth/api/device/token* {
    rewrite * /oauth/device/token
    reverse_proxy https://api.wetrakr.com {
        header_up Host api.wetrakr.com
    }
}

handle /wetrakr-oauth* {
    root * /var/www/html
    header Cache-Control "no-store"
    header Referrer-Policy "no-referrer"
    header X-Content-Type-Options "nosniff"
    file_server
}
```

And mounted in docker-compose:

```yaml
- ./wetrakr-oauth/static:/var/www/html/wetrakr-oauth:ro
```

## Files

- `static/index.html` — Static device-code-flow page (the active implementation, strict CSP)
- `templates/index.html` — Same page for local Flask dev (`flask run`), relaxed CSP
- `wetrakr_oauth/app.py` — Minimal Flask app: serves the dev template and health checks only.
  It is not in the request path when deployed behind Caddy as above.
