# Plex OAuth - Kometa

A static, fully client-side web page for authenticating with Plex and obtaining an access token for use with [Kometa](https://kometa.wiki/).

**No user-sensitive data touches the server.** The page is served as a static file; every Plex API call is a `fetch()` straight from the user's browser to `plex.tv` (which is CORS-enabled), so requests also originate from the user's IP — avoiding Plex's server-IP warning. The resulting token never transits or is stored by this server.

## Features

- Clean, modern UI with Plex branding
- Browser-only PIN-based OAuth flow (no redirect, no callback route)
- Account information display after authentication (username, email, admin status, avatar)
- Privacy headers: `Cache-Control: no-store`, `Referrer-Policy: no-referrer`, and a restrictive Content-Security-Policy (`connect-src https://plex.tv`)
- One-click token copy to clipboard

## How It Works

1. The page POSTs to `https://plex.tv/api/v2/pins` to create a strong PIN.
2. The user opens the displayed `app.plex.tv/auth` link, signs in, and authorizes the app.
3. The page polls `https://plex.tv/api/v2/pins/<id>` until the PIN is authorized and returns an `authToken`.
4. The page fetches account info from `https://plex.tv/api/v2/user` and displays the token for copying into the Kometa `config.yml` (`plex:` → `token:`).

## Files

- `static/index.html` — the entire application (HTML + CSS + JS, no dependencies)
- `plex_oauth/`, `templates/`, `application.py`, `Dockerfile`, `Procfile` — legacy Flask implementation, retained as a fallback but no longer routed

## Deployment

The page is served by the Caddy container, which mounts `./plex-oauth/static` at `/var/www/html/plex-oauth` (see `docker-compose.yml`). The Caddyfile serves the static page with security headers:

```caddy
handle /plex-oauth* {
    root * /var/www/html
    header Cache-Control "no-store"
    header Referrer-Policy "no-referrer"
    header X-Content-Type-Options "nosniff"
    file_server
}
```

There is no callback URL to register — Plex's PIN flow does not use redirects.

No container, Python environment, or environment variables are required for the static page.

## Legacy Flask App (fallback)

The previous Flask service remains in this directory and in `docker-compose.yml` but receives no traffic once Caddy serves the static page. (The Flask app was already a thin shell that only rendered the same client-side page.) To roll back, restore the old `handle /plex-oauth*` reverse-proxy block in the Caddyfile.

Environment variables used by the legacy app only:

- `PORT` - Port to run on (default: 8080)
- `HOST` - Host to bind to (default: 127.0.0.1)
- `DEBUG` - Enable debug mode (default: False)
- `SECRET_KEY` - Flask secret key (default: dev-key-change-in-production)

## Links

- [Plex OAuth Documentation](https://forums.plex.tv/t/authenticating-with-plex/609370)
- [Kometa Documentation](https://kometa.wiki/)

## License

MIT License - see [LICENSE](LICENSE) file for details
