"""
Trakt OAuth Flask Application.

A minimal Flask web application for authenticating with Trakt and obtaining access tokens.
"""

import os
import secrets
import time
from threading import Lock
from urllib.parse import urlencode

import requests  # type: ignore[import-untyped]
from flask import Flask, jsonify, make_response, render_template, request

app = Flask(__name__, template_folder="../templates")
app.secret_key = os.getenv("SECRET_KEY", "dev-key-change-in-production")

# Trakt API Configuration
TRAKT_API_URL = "https://api.trakt.tv"
TRAKT_AUTH_URL = "https://trakt.tv/oauth"
TRAKT_REDIRECT_URI = os.getenv("TRAKT_REDIRECT_URI", "http://localhost:8080/callback")
AUTHORIZATION_TTL_SECONDS = 600

# Official Kometa Trakt app credentials, held server-side only. These are used
# by the /api/official/* endpoints so the client_secret never reaches the
# browser. Leave empty to disable the official flow.
CLIENT_ID = os.getenv("CLIENT_ID", "")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")
pending_authorizations = {}
pending_authorizations_lock = Lock()


def render_index(callback_result=None, callback_error=None, status=200):
    """Render the OAuth page without allowing sensitive results to be cached."""
    response = make_response(
        render_template(
            "index.html",
            redirect_uri=TRAKT_REDIRECT_URI,
            callback_result=callback_result,
            callback_error=callback_error,
        ),
        status,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def remove_expired_authorizations(now):
    """Discard abandoned OAuth attempts before storing or consuming state."""
    expired_states = [
        state
        for state, authorization in pending_authorizations.items()
        if now - authorization["created_at"] > AUTHORIZATION_TTL_SECONDS
    ]
    for state in expired_states:
        pending_authorizations.pop(state, None)


def build_authorization_url(client_id, state):
    """Build the Trakt authorization URL for a given client and state."""
    return f"{TRAKT_AUTH_URL}/authorize?" + urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": TRAKT_REDIRECT_URI,
            "state": state,
        }
    )


def exchange_code_for_token(client_id, client_secret, code, redirect_uri):
    """Exchange authorization code for access token."""
    try:
        response = requests.post(
            f"{TRAKT_AUTH_URL}/token",
            json={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            headers={
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"Error exchanging code: {e}")
        return None


@app.route("/")
def index():
    """Render the main page."""
    return render_index()


@app.route("/api/start", methods=["POST"])
def start_authorization():
    """Create a short-lived authorization request and return the Trakt URL."""
    data = request.get_json(silent=True) or {}
    client_id = data.get("client_id", "").strip()
    client_secret = data.get("client_secret", "").strip()

    if not client_id or not client_secret:
        return jsonify({"error": "Client ID and Client Secret are required"}), 400

    state = secrets.token_urlsafe(32)
    now = time.monotonic()
    with pending_authorizations_lock:
        remove_expired_authorizations(now)
        pending_authorizations[state] = {
            "client_id": client_id,
            "client_secret": client_secret,
            "created_at": now,
        }

    return jsonify({"authorization_url": build_authorization_url(client_id, state)})


@app.route("/callback")
def authorization_callback():
    """Validate one-time state and exchange Trakt's callback code."""
    state = request.args.get("state", "")
    code = request.args.get("code", "")
    trakt_error = request.args.get("error", "")

    if not state:
        return render_index(callback_error="Missing authorization state.", status=400)

    now = time.monotonic()
    with pending_authorizations_lock:
        remove_expired_authorizations(now)
        authorization = pending_authorizations.pop(state, None)

    if authorization is None:
        return render_index(
            callback_error="This authorization request is invalid, expired, or already used.",
            status=400,
        )
    if trakt_error:
        return render_index(callback_error=f"Trakt authorization failed: {trakt_error}")
    if not code:
        return render_index(
            callback_error="Trakt did not return an authorization code.", status=400
        )

    token_data = exchange_code_for_token(
        authorization["client_id"],
        authorization["client_secret"],
        code,
        TRAKT_REDIRECT_URI,
    )
    if not token_data:
        return render_index(
            callback_error="Token exchange failed. Verify the client credentials and callback URI.",
            status=400,
        )

    return render_index(
        callback_result={
            "client_id": authorization["client_id"],
            "client_secret": authorization["client_secret"],
            "token_data": token_data,
        }
    )


@app.route("/api/exchange-code", methods=["POST"])
def exchange_code():
    """Exchange a Trakt authorization code for an access token."""
    try:
        data = request.json
        if not data:
            return jsonify({"error": "No JSON data provided"}), 400

        client_id = data.get("client_id", "").strip()
        client_secret = data.get("client_secret", "").strip()
        code = data.get("code", "").strip()
        if not all([client_id, client_secret, code]):
            return (
                jsonify({"error": "Missing required parameters (client_id, client_secret, code)"}),
                400,
            )

        token_data = exchange_code_for_token(client_id, client_secret, code, TRAKT_REDIRECT_URI)
        if not token_data:
            return (
                jsonify({"error": "Failed to exchange code for token. Check your credentials."}),
                500,
            )

        if "error" in token_data:
            error_msg = token_data.get(
                "error_description", token_data.get("error", "Unknown error")
            )
            return jsonify({"error": f"Trakt error: {error_msg}"}), 400

        return jsonify(
            {
                "success": True,
                "access_token": token_data.get("access_token"),
                "refresh_token": token_data.get("refresh_token"),
                "expires_in": token_data.get("expires_in"),
                "token_type": token_data.get("token_type", "Bearer"),
                "created_at": token_data.get("created_at"),
            }
        )
    except Exception as e:
        print(f"Error in exchange_code: {e}")
        return jsonify({"error": f"Server error: {str(e)}"}), 500


@app.route("/api/official/start", methods=["POST"])
def official_start_authorization():
    """Start an authorization with the server-held official credentials.

    The client_secret never leaves this process; the browser only receives the
    authorization URL and a single-use state to present on the callback.
    """
    if not CLIENT_ID or not CLIENT_SECRET:
        return (
            jsonify({"error": "The Kometa Trakt app is not configured."}),
            503,
        )

    state = secrets.token_urlsafe(32)
    now = time.monotonic()
    with pending_authorizations_lock:
        remove_expired_authorizations(now)
        pending_authorizations[state] = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "created_at": now,
        }

    return jsonify({"authorization_url": build_authorization_url(CLIENT_ID, state)})


@app.route("/api/official/exchange", methods=["POST"])
def official_exchange_code():
    """Exchange an authorization code using the official credentials.

    The browser supplies only the code and the state issued by
    /api/official/start; the client_id/client_secret come from this process.
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
                jsonify(
                    {"error": "This authorization request is invalid, expired, or already used."}
                ),
                400,
            )

        token_data = exchange_code_for_token(
            authorization["client_id"],
            authorization["client_secret"],
            code,
            TRAKT_REDIRECT_URI,
        )
        if not token_data:
            return (
                jsonify({"error": "Failed to exchange code for token. Check server logs."}),
                500,
            )

        if "error" in token_data:
            error_msg = token_data.get(
                "error_description", token_data.get("error", "Unknown error")
            )
            return jsonify({"error": f"Trakt error: {error_msg}"}), 400

        return jsonify(
            {
                "success": True,
                "client_id": authorization["client_id"],
                "client_secret": authorization["client_secret"],
                "access_token": token_data.get("access_token"),
                "refresh_token": token_data.get("refresh_token"),
                "expires_in": token_data.get("expires_in"),
                "token_type": token_data.get("token_type", "Bearer"),
                "created_at": token_data.get("created_at"),
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
