# FlickList OAuth - Kometa (Static Device-Code Flow)

A fully client-side FlickList auth page using FlickList's device-code flow (`credential: "key"`).
The browser never talks to `flicklist.tv` directly — FlickList's `/api/auth/device/*` endpoints send
no CORS headers (confirmed via a live OPTIONS preflight; no `Access-Control-Allow-Origin` in the
response), so a direct `fetch()` from the browser would fail. The page instead POSTs to a same-origin
path that Caddy forwards to FlickList untouched — see the Caddyfile block below. The resulting API key
never touches this server either way; no `client_secret` is used anywhere in this flow.

## How It Works

1. User clicks "Connect with FlickList"
2. Page requests a code via `POST /flicklist-oauth/api/device/code` (proxied to
   `POST https://flicklist.tv/api/auth/device/code` with `{"client_id": ..., "credential": "key"}`)
3. User opens the FlickList link shown and enters the code
4. Page polls `POST /flicklist-oauth/api/device/token` (proxied to
   `POST https://flicklist.tv/api/auth/device/token`) until FlickList returns the key
5. Configuration is displayed for copying into Kometa's `config.yml`

FlickList API keys are permanent (`expires_at: null`) and are not individually revocable — running
this flow again for the same account rotates the key in place rather than minting a second one
alongside it. The page says so before the user starts.

## Configuration

`FLICKLIST_CLIENT_ID` is a public identifier (no `client_secret` is involved anywhere in this flow),
so it's a committed constant directly in `static/index.html`, matching the current SIMKL/Plex pattern
in this repo rather than an env var.

**Before this page ships for real**, that constant needs the production value. It currently holds
`kometa_integration_test_9c51098d`, the test registration used during API validation — good enough
to exercise this page end-to-end, but not the one that ships. A Kometa-Team FlickList developer
account still needs to register its own app (see the FlickList Developer → Your Apps dashboard) and
swap its client_id in; this is a currently-open item (who owns that account is undecided too).

## Deployment

The page is served as static content by Caddy, with a same-origin passthrough for the two device
endpoints (FlickList's own CORS gap is the reason the passthrough exists — see MAL's equivalent
`/mal-oauth/api/token` block for the established precedent in this repo):

```caddy
handle /flicklist-oauth/api/device/code* {
    rewrite * /api/auth/device/code
    reverse_proxy https://flicklist.tv {
        header_up Host flicklist.tv
    }
}

handle /flicklist-oauth/api/device/token* {
    rewrite * /api/auth/device/token
    reverse_proxy https://flicklist.tv {
        header_up Host flicklist.tv
    }
}

handle /flicklist-oauth* {
    root * /var/www/html
    header Cache-Control "no-store"
    header Referrer-Policy "no-referrer"
    header X-Content-Type-Options "nosniff"
    file_server
}
```

And mounted in docker-compose:

```yaml
- ./flicklist-oauth/static:/var/www/html/flicklist-oauth:ro
```

## Files

- `static/index.html` — Static device-code-flow page (the active implementation, strict CSP)
- `templates/index.html` — Same page for local Flask dev (`flask run`), relaxed CSP
- `flicklist_oauth/app.py` — Minimal Flask app: serves the dev template and health checks only.
  It is not in the request path when deployed behind Caddy as above.
