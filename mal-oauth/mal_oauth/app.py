"""MyAnimeList OAuth Flask Application.

A minimal Flask web application for authenticating with MyAnimeList and
obtaining access tokens.
"""

import os
import secrets
from pathlib import Path

import requests  # type: ignore[import-untyped]
from flask import Flask, jsonify, render_template, send_file

app = Flask(__name__, template_folder="../templates")
app.secret_key = os.getenv("SECRET_KEY", "dev-key-change-in-production")

# MAL API Configuration
MAL_API_URL = "https://myanimelist.net/v1/oauth2"
ROOT_PATH = os.getenv("ROOT_PATH", "")
LOGO_PATH = Path(__file__).resolve().parent.parent / "MyAnimeList_Logo.svg"


def generate_pkce_pair():
    """Generate PKCE code verifier."""
    code_verifier = secrets.token_urlsafe(100)[:128]
    return code_verifier


def exchange_code_for_token(client_id, client_secret, code, code_verifier):
    """Exchange authorization code for access token."""
    try:
        response = requests.post(
            f"{MAL_API_URL}/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "code_verifier": code_verifier,
                "grant_type": "authorization_code",
            },
            timeout=10,
        )
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

        token_data = exchange_code_for_token(client_id, client_secret, code, code_verifier)
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
