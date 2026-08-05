# SIMKL OAuth - Kometa (Static PIN Flow)

A fully client-side SIMKL OAuth page using the PIN (device) flow.
The browser talks directly to `api.simkl.com` (CORS-enabled); the user token never touches this server.

## How It Works

1. User clicks "Connect with SIMKL"
2. Page requests a PIN code from SIMKL via `POST /oauth/pin`
3. User opens `simkl.com/pin`, signs in, and enters the code
4. Page polls `GET /oauth/pin/check/{code}` until SIMKL returns the `access_token`
5. Configuration is displayed for copying into Kometa's `config.yml`

## Configuration

Only `SIMKL_CLIENT_ID` is needed (public identifier — already visible in the legacy site's authorize URL).
It is injected into `static/index.html` at build/deploy time from `.env`.

```bash
SIMKL_CLIENT_ID=your_simkl_client_id
```

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

## Legacy Fallback

The original Flask app (`simkl_oauth/app.py`) is retained for backwards compatibility but is no longer the primary route. It uses OAuth 2.0 authorization-code flow requiring `CLIENT_ID`, `CLIENT_SECRET`, and `REDIRECT_URI`.

## Files

- `static/index.html` — Static PIN-flow page (the active implementation)
- `static/logo.svg` — SIMKL logo for landing page integration
- `simkl_oauth/app.py` — Legacy Flask app (fallback only)
