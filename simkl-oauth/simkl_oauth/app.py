"""SIMKL OAuth Flask Application.

A minimal Flask web application for authenticating with SIMKL and obtaining
access tokens for use with Kometa.
"""

import os
from pathlib import Path
from urllib.parse import urlencode

import requests  # type: ignore[import-untyped]
from dotenv import load_dotenv
from flask import Flask, render_template, request, send_file

load_dotenv()

# Validate required env vars at startup
_CLIENT_ID = os.getenv("CLIENT_ID", "")
_CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")
_REDIRECT_URI = os.getenv("REDIRECT_URI", "")

_missing = [
    name
    for name, val in [
        ("CLIENT_ID", _CLIENT_ID),
        ("CLIENT_SECRET", _CLIENT_SECRET),
        ("REDIRECT_URI", _REDIRECT_URI),
    ]
    if not val
]
if _missing:
    raise RuntimeError(f"Missing required environment variables: {', '.join(_missing)}")

CLIENT_ID: str = _CLIENT_ID
CLIENT_SECRET: str = _CLIENT_SECRET
REDIRECT_URI: str = _REDIRECT_URI

SIMKL_AUTH_URL = "https://simkl.com/oauth/authorize"
SIMKL_TOKEN_URL = "https://api.simkl.com/oauth/token"  # nosec B105
ROOT_PATH = os.getenv("ROOT_PATH", "")
LOGO_PATH = Path(__file__).resolve().parent.parent / "static" / "simkl-logo.svg"

SIMKL_API_PARAMS = {"client_id": CLIENT_ID, "app-name": "kometa", "app-version": "1.0"}
SIMKL_HEADERS = {"Content-Type": "application/json", "User-Agent": "Kometa-Utilities/1.0"}


def exchange_code_for_token(code: str):
    """Exchange authorization code for SIMKL access token.

    Returns parsed JSON dict on success.
    Returns dict with 'error' key on HTTP error (non-2xx response).
    Returns None on connection/unexpected errors.
    """
    try:
        response = requests.post(
            SIMKL_TOKEN_URL,
            params=SIMKL_API_PARAMS,
            json={
                "code": code,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "redirect_uri": REDIRECT_URI,
                "grant_type": "authorization_code",
            },
            headers=SIMKL_HEADERS,
            timeout=10,
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.HTTPError as e:
        resp = e.response if hasattr(e, "response") and e.response is not None else None
        status = resp.status_code if resp is not None else "?"
        body = resp.text if resp is not None else str(e)
        print(f"SIMKL API HTTP Error: {e}")
        return {"error": f"{status}: {body}"}
    except Exception as e:
        print(f"Error exchanging code: {e}")
        return None


app = Flask(__name__, template_folder="../templates")


@app.context_processor
def inject_root_path() -> dict:
    """Inject ROOT_PATH into all templates."""
    return {"root_path": ROOT_PATH}


@app.route("/")
def index():
    """Render the main page."""
    auth_url = f"{SIMKL_AUTH_URL}?{urlencode({'response_type': 'code', 'redirect_uri': REDIRECT_URI, **SIMKL_API_PARAMS})}"
    return render_template("index.html", state="default", auth_url=auth_url)


@app.route("/logo.svg")
def logo():
    """Return the Simkl logo."""
    response = send_file(LOGO_PATH, mimetype="image/svg+xml", max_age=86400)
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response


@app.route("/callback")
def callback():
    """Handle SIMKL OAuth callback."""
    error = request.args.get("error")
    if error:
        error_description = request.args.get("error_description", error)
        return render_template("index.html", state="error", error_message=error_description)

    code = request.args.get("code")
    if not code:
        return render_template(
            "index.html", state="error", error_message="No authorization code received."
        )

    token_data = exchange_code_for_token(code)
    if token_data is None:
        return render_template(
            "index.html",
            state="error",
            error_message="Failed to connect to SIMKL. Please try again.",
        )

    if "error" in token_data:
        return render_template("index.html", state="error", error_message=token_data["error"])

    access_token = token_data.get("access_token")
    if not access_token:
        return render_template(
            "index.html", state="error", error_message="Unexpected response from SIMKL."
        )

    return render_template("index.html", state="success", user_token=access_token)


@app.route("/api/health", methods=["GET"])
@app.route("/health/live", methods=["GET"])
@app.route("/health/ready", methods=["GET"])
def health():
    """Health check endpoint."""
    return {"status": "ok"}


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    debug = os.getenv("DEBUG", "False").lower() == "true"
    host = os.getenv("HOST", "127.0.0.1")
    app.run(host=host, port=port, debug=debug)
