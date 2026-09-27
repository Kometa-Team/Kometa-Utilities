# SIMKL OAuth - Kometa (Static Device Flow, AUTH V2)

A fully client-side SIMKL OAuth page using the AUTH V2 device flow (RFC 8628).
The browser talks directly to `api.simkl.com` (CORS-enabled); the user tokens never touch this server.

## How It Works

1. User clicks "Connect with SIMKL"
2. Page requests a device code via `POST /oauth2/device` (`client_id`, `scope=media:read media:write`)
3. User opens `verification_uri_complete` (`simkl.com/pin?user_code=...`), signs in, and approves.
   The code is also shown on the page for approving on a different device.
4. Page polls `POST /oauth2/token` with
   `grant_type=urn:ietf:params:oauth:grant-type:device_code` until SIMKL returns tokens
5. Configuration is displayed for copying into Kometa's `config.yml`

The device flow needs **no `client_secret` and no PKCE** — only the public `client_id`.

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

## Legacy Flask app (dead code)

`simkl_oauth/app.py` implements the AUTH V1 authorization-code flow (`/oauth/authorize`,
`/oauth/token`) requiring `CLIENT_ID`, `CLIENT_SECRET` and `REDIRECT_URI`.

It is **not deployed and not reachable**: Caddy serves `/simkl-oauth*` as static files with
no `reverse_proxy`, and no `simkl-oauth` container runs. It has not been migrated to AUTH V2
and would stop working when SIMKL retires V1 (expected around April 2027).

## Files

- `static/index.html` — Static PIN-flow page (the active implementation)
- `static/logo.svg` — SIMKL logo for landing page integration
- `simkl_oauth/app.py` — Legacy Flask app (fallback only)
