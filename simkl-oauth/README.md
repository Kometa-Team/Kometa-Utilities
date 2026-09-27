# SIMKL OAuth - Kometa (Static Device Flow, AUTH V2)

A fully client-side SIMKL OAuth page using the AUTH V2 device flow (RFC 8628).
The browser talks directly to `api.simkl.com` (CORS-enabled); the user tokens never touch this server.

## How It Works

1. User clicks "Connect with SIMKL"
2. Page requests a device code via `POST /oauth2/device` (`client_id`, `scope=media:read media:write`)
3. User opens `verification_uri_complete` (`simkl.com/pin?user_code=...`), signs in, and approves.
   The code is also shown on the page for approving on a different device.
4. Page polls `POST /simkl-oauth/api/official/token` (this service) with the
   `device_code`; it forwards to SIMKL's `POST /oauth2/token` with the
   `urn:ietf:params:oauth:grant-type:device_code` grant until SIMKL returns tokens
5. Configuration is displayed for copying into Kometa's `config.yml`

The device flow needs no PKCE. It also needs no `client_secret` *at the device-code step* —
but SIMKL rejects the final token exchange for the shared Kometa client with
`invalid_client` / "Client authentication failed" unless the secret is supplied. A secret
cannot live in a public static page, so step 4 is proxied through this service, which holds
`CLIENT_SECRET` in its environment and passes SIMKL's status and body straight back.

That means the user's tokens transit this server. They are not stored, and callers cannot
supply their own client credentials — the proxy always injects the server's.

## Tokens

AUTH V2 access tokens expire after **7 days**; refresh tokens last **180 days** (sliding).
The page therefore emits the **refresh token**, which Kometa exchanges for access tokens itself:

```yaml
simkl:
  refresh_token: simkl_rt_...
```

## Configuration

Only `SIMKL_CLIENT_ID` is needed (a public identifier, not a secret). It must be an
**AUTH V2** client — a v1 `client_id` is rejected by the `/oauth2` endpoints.

It is currently hardcoded in `static/index.html` (search for `SIMKL_CLIENT_ID`); the
`.env`/compose injection described previously was never wired up.

## Deployment

The page is served as static content by Caddy:

```caddy
handle /simkl-oauth* {
    root * /var/www/html
    header Cache-Control "no-store"
    header Referrer-Policy "no-referrer"
    header X-Content-Type-Options "nosniff"
    file_server
}
```

And mounted in docker-compose:

```yaml
- ./simkl-oauth/static:/var/www/html/simkl-oauth:ro
```

## The Flask service

`simkl_oauth/app.py` is now a single-purpose token-exchange proxy. It exposes:

- `POST /api/official/token` — body `{"device_code": "..."}`; adds `client_id` and
  `client_secret` and forwards to SIMKL, returning SIMKL's status and JSON unchanged
- `GET /logo.svg`, `GET /api/health`, `GET /health/live`, `GET /health/ready`

It no longer serves any HTML: the user-facing page is the static one above. The previous
AUTH V1 authorization-code implementation (`/oauth/authorize`, `/oauth/token`, `REDIRECT_URI`)
has been removed.

Caddy routes only `/simkl-oauth/api/official/*` to this container; everything else under
`/simkl-oauth` is served as static files.

## Files

- `static/index.html` — Static PIN-flow page (the active implementation)
- `static/logo.svg` — SIMKL logo for landing page integration
- `simkl_oauth/app.py` — Legacy Flask app (fallback only)
