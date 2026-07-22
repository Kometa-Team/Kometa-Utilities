# Trakt OAuth - Kometa

A simple Flask web application for authenticating with Trakt and obtaining access tokens for use with Kometa.

## Features

- Clean, modern UI with Trakt branding
- Step-by-step authentication process
- Automatic configuration generation for Kometa
- Copy-to-clipboard functionality
- Secure token exchange
- Single-use, ten-minute callback validation

## Usage

1. Visit the application
2. Register the displayed callback URI in your Trakt application
3. Enter your Trakt Client ID and Client Secret
4. Click "Continue to Trakt" and approve access
5. Copy the generated configuration into your Kometa `config.yml`

## Running Locally

### Python
```bash
pip install -r requirements.txt
python app.py
```

Visit `http://localhost:8080`

### Docker
```bash
docker build -t trakt-oauth .
docker run -p 8080:5000 \
  -e TRAKT_REDIRECT_URI=http://localhost:8080/callback \
  trakt-oauth
```

## Environment Variables

- `PORT` - Port to run on (default: 8080)
- `HOST` - Host to bind to (default: 127.0.0.1)
- `DEBUG` - Enable debug mode (default: False)
- `SECRET_KEY` - Flask secret key (default: dev-key-change-in-production)
- `TRAKT_REDIRECT_URI` - Exact callback URI registered in the Trakt application (default: `http://localhost:8080/callback`)

## Deployment

This service is designed to be deployed behind a reverse proxy like Caddy.
Pending authorization state is held in process memory for ten minutes. Run one
Gunicorn worker, as configured in the included Dockerfile, unless this state is
moved to a shared store.

### With Caddy
```caddy
trakt-oauth.example.com {
    reverse_proxy http://127.0.0.1:8080
}
```
