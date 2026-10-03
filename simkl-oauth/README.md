# SIMKL OAuth - Kometa (Static Device Flow, AUTH V2)

A fully client-side SIMKL OAuth page using the AUTH V2 device flow (RFC 8628).
The browser talks directly to `api.simkl.com` (CORS-enabled) for every step,
including the final token exchange; no server of ours is involved and no
token is stored.

## How It Works

1. User clicks "Connect with SIMKL"
2. Page requests a device code via `POST /oauth2/device` (`client_id`, `scope=media:read media:write`)
3. User opens `verification_uri_complete` (`simkl.com/pin?user_code=...`), signs in, and approves.
   The code is also shown on the page for approving on a different device.
4. Page polls `POST /oauth2/token` directly on `api.simkl.com` with the
   `client_id` and `device_code`, using the
   `urn:ietf:params:oauth:grant-type:device_code` grant, until SIMKL returns tokens
5. Configuration is displayed for copying into Kometa's `config.yml`

This client is registered for the no-secret device grant, so the token exchange
needs only the public `client_id` — no `client_secret` anywhere in the flow, and
nothing for a backend to hold.

## Tokens

AUTH V2 access tokens expire after **7 days**; refresh tokens last **180 days** (sliding).
The page therefore emits the **refresh token**, which Kometa exchanges for access tokens itself:

```yaml
simkl:
  refresh_token: simkl_rt_...
```

## Configuration

Only a `client_id` is needed (a public identifier, not a secret). It must be an
**AUTH V2** client registered for the no-secret device grant — a v1 `client_id`,
or a v2 client still requiring a secret for this grant, is rejected by the
`/oauth2` endpoints.

It is hardcoded in `static/index.html` (search for `SIMKL_CLIENT_ID`). There is
no backend and no environment configuration.

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

## Files

- `static/index.html` — Static PIN-flow page (the entire implementation)
- `static/logo.svg` — SIMKL logo for landing page integration
