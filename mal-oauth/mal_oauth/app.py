"""MyAnimeList OAuth Flask Application.

A minimal Flask web application for authenticating with MyAnimeList and
obtaining access tokens.
"""

import os
import secrets
import time
from pathlib import Path
from threading import Lock
from urllib.parse import urlencode, urlsplit

import requests  # type: ignore[import-untyped]
from flask import Flask, jsonify, render_template, request, send_file

app = Flask(__name__, template_folder="../templates")
app.secret_key = os.getenv("SECRET_KEY", "dev-key-change-in-production")

# MAL API Configuration
MAL_API_URL = "https://myanimelist.net/v1/oauth2"
ROOT_PATH = os.getenv("ROOT_PATH", "")
LOGO_PATH = Path(__file__).resolve().parent.parent / "static" / "myanimelist-logo.svg"

# Official Kometa MAL app credentials, held server-side only. MAL supports
# PKCE without a client_secret, so only CLIENT_ID is required for the official
# flow; CLIENT_SECRET is kept as a fallback for any future non-PKCE needs.
CLIENT_ID = os.getenv("CLIENT_ID", "")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")
MAL_REDIRECT_URI = os.getenv(
    "MAL_REDIRECT_URI", "https://utilities.kometa.wiki/mal-oauth/callback"
)
AUTHORIZATION_TTL_SECONDS = 600
pending_authorizations = {}
pending_authorizations_lock = Lock()


def remove_expired_authorizations(now):
    """Discard abandoned official OAuth attempts before storing or consuming state."""
    expired_states = [
        state
        for state, authorization in pending_authorizations.items()
        if now - authorization["created_at"] > AUTHORIZATION_TTL_SECONDS
    ]
    for state in expired_states:
        pending_authorizations.pop(state, None)


def generate_pkce_pair():
    """Generate PKCE code verifier."""
    code_verifier = secrets.token_urlsafe(100)[:128]
    return code_verifier


def exchange_code_for_token(client_id, client_secret, code, code_verifier, redirect_uri):
    """Exchange authorization code for access token.

    client_secret is optional: MAL's PKCE flow works without it, and the
    official flow intentionally omits it so the secret never leaves the server.
    redirect_uri must exactly match the value used in the authorization
    request; MAL rejects the exchange otherwise.
    """
    try:
        data = {
            "client_id": client_id,
            "code": code,
            "code_verifier": code_verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
        if client_secret:
            data["client_secret"] = client_secret
        response = requests.post(f"{MAL_API_URL}/token", data=data, timeout=10)
        print(f"MAL API Response Status: {response.status_code}")
        print(f"MAL API Response Body: {response.text}")
        response.raise_for_status()
        return response.json()
    except requests.exceptions.HTTPError as e:
        print(f"MAL API HTTP Error: {e}")
        print(f"Response: {e.response.text if hasattr(e, 'response') else 'No response'}")
        return {"error": f"MAL API error: {e.response.text if hasattr(e, 'response') else str(e)}"}
    except Exception as e:
        print(f"Error exchanging code: {e}")
        return None


@app.route("/")
def index():
    """Render the main page."""
    code_verifier = generate_pkce_pair()
    return render_template("index.html", code_verifier=code_verifier, root_path=ROOT_PATH)


@app.route("/logo.svg")
def logo():
    """Return the MyAnimeList logo."""
    response = send_file(LOGO_PATH, mimetype="image/svg+xml", max_age=86400)
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response


@app.route("/api/exchange-code", methods=["POST"])
def exchange_code():
    """Exchange MAL authorization code for access token."""
    from flask import request

    try:
        data = request.json
        if not data:
            return jsonify({"error": "No JSON data provided"}), 400

        client_id = data.get("client_id", "").strip()
        client_secret = data.get("client_secret", "").strip()
        localhost_url = data.get("localhost_url", "").strip()
        code_verifier = data.get("code_verifier", "").strip()

        print(f"Received request - Client ID: {client_id[:8]}..., URL: {localhost_url}")

        if not all([client_id, client_secret, localhost_url, code_verifier]):
            return jsonify({"error": "Missing required parameters"}), 400

        # Extract code from localhost URL
        import re

        match = re.search(r"code=([^&]+)", localhost_url)
        if not match:
            return jsonify({"error": "Could not find authorization code in URL"}), 400

        code = match.group(1)
        print(f"Extracted code: {code[:10]}...")

        # The redirect_uri MAL redirected to (the path of localhost_url) must be
        # echoed back during the exchange; MAL rejects mismatches.
        redirect_uri = urlsplit(localhost_url)._replace(query="", fragment="").geturl()

        token_data = exchange_code_for_token(
            client_id, client_secret, code, code_verifier, redirect_uri
        )
        if not token_data:
            return jsonify({"error": "Failed to exchange code for token. Check server logs."}), 500

        if "error" in token_data:
            error_msg = token_data.get("error", "Authentication failed")
            print(f"Token exchange error: {error_msg}")
            return jsonify({"error": error_msg}), 400

        return jsonify(
            {
                "success": True,
                "access_token": token_data.get("access_token"),
                "refresh_token": token_data.get("refresh_token"),
                "expires_in": token_data.get("expires_in"),
                "token_type": token_data.get("token_type", "Bearer"),
                "localhost_url": localhost_url,
            }
        )
    except Exception as e:
        print(f"Error in exchange_code endpoint: {e}")
        import traceback

        traceback.print_exc()
        return jsonify({"error": f"Server error: {str(e)}"}), 500


def build_authorization_url(client_id, state, code_challenge):
    """Build the MAL authorization URL (PKCE 'plain' challenge, the only method MAL supports)."""
    return f"{MAL_API_URL}/authorize?" + urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": MAL_REDIRECT_URI,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "plain",
        }
    )


@app.route("/api/official/start", methods=["POST"])
def official_start_authorization():
    """Start an authorization with the server-held official credentials.

    The PKCE code_verifier and single-use state are generated and stored
    server-side; the browser only receives the authorization URL, so neither
    the client_id nor the verifier has to be embedded in the page.
    """
    if not CLIENT_ID:
        return (
            jsonify({"error": "The Kometa MAL app is not configured."}),
            503,
        )

    state = secrets.token_urlsafe(32)
    code_verifier = generate_pkce_pair()
    now = time.monotonic()
    with pending_authorizations_lock:
        remove_expired_authorizations(now)
        pending_authorizations[state] = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code_verifier": code_verifier,
            "created_at": now,
        }

    return jsonify(
        {"authorization_url": build_authorization_url(CLIENT_ID, state, code_verifier)}
    )


@app.route("/api/official/exchange", methods=["POST"])
def official_exchange_code():
    """Exchange an authorization code using the official credentials.

    The browser supplies only the code and the state issued by
    /api/official/start; the exchange uses the server-held client_id and the
    server-generated PKCE verifier, with no client_secret involved.
    """
    try:
        data = request.get_json(silent=True) or {}
        code = data.get("code", "").strip()
        state = data.get("state", "").strip()
        if not code or not state:
            return (
                jsonify({"error": "Missing required parameters (code, state)"}),
                400,
            )

        now = time.monotonic()
        with pending_authorizations_lock:
            remove_expired_authorizations(now)
            authorization = pending_authorizations.pop(state, None)

        if authorization is None:
            return (
                jsonify({"error": "This authorization request is invalid, expired, or already used."}),
                400,
            )

        token_data = exchange_code_for_token(
            authorization["client_id"],
            authorization["client_secret"],
            code,
            authorization["code_verifier"],
            MAL_REDIRECT_URI,
        )
        if not token_data:
            return (
                jsonify({"error": "Failed to exchange code for token. Check server logs."}),
                500,
            )

        if "error" in token_data:
            error_msg = token_data.get("error", "Authentication failed")
            return jsonify({"error": error_msg}), 400

        return jsonify(
            {
                "success": True,
                "client_id": authorization["client_id"],
                "access_token": token_data.get("access_token"),
                "refresh_token": token_data.get("refresh_token"),
                "expires_in": token_data.get("expires_in"),
                "token_type": token_data.get("token_type", "Bearer"),
            }
        )
    except Exception as e:
        print(f"Error in official_exchange_code: {e}")
        return jsonify({"error": f"Server error: {str(e)}"}), 500


@app.route("/api/health", methods=["GET"])
@app.route("/health/live", methods=["GET"])
@app.route("/health/ready", methods=["GET"])
def health():
    """Health check endpoint."""
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    debug = os.getenv("DEBUG", "False").lower() == "true"
    host = os.getenv("HOST", "127.0.0.1")
    app.run(host=host, port=port, debug=debug)
