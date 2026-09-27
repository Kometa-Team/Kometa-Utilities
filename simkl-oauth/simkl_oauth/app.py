"""SIMKL OAuth token-exchange proxy (AUTH V2 device flow).

The user-facing page (``static/index.html``) is served directly by Caddy and
runs the whole device flow in the browser. It cannot perform the final token
exchange itself: SIMKL's ``/oauth2/token`` rejects the shared Kometa client
with ``invalid_client`` / "Client authentication failed" unless the client
secret is supplied, and a secret cannot live in a public static page.

This service exists only to add that secret. It holds ``CLIENT_ID`` and
``CLIENT_SECRET`` in its environment, accepts a ``device_code`` from the
browser, and forwards the exchange to SIMKL. Neither the device code nor the
resulting tokens are stored.

Callers may not supply their own client credentials: the endpoint injects the
server's, so it cannot be used as an open relay for arbitrary SIMKL apps.
"""

import os
from pathlib import Path

import requests  # type: ignore[import-untyped]
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_file

load_dotenv()

# The device flow needs no REDIRECT_URI: there is no browser redirect.
_CLIENT_ID = os.getenv("CLIENT_ID", "")
_CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")

_missing = [
    name for name, val in [("CLIENT_ID", _CLIENT_ID), ("CLIENT_SECRET", _CLIENT_SECRET)] if not val
]
if _missing:
    raise RuntimeError(f"Missing required environment variables: {', '.join(_missing)}")

CLIENT_ID: str = _CLIENT_ID
CLIENT_SECRET: str = _CLIENT_SECRET

SIMKL_TOKEN_URL = "https://api.simkl.com/oauth2/token"  # nosec B105
DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"  # nosec B105
ROOT_PATH = os.getenv("ROOT_PATH", "")
LOGO_PATH = Path(__file__).resolve().parent.parent / "static" / "simkl-logo.svg"

REQUEST_TIMEOUT = 10

app = Flask(__name__)


@app.route("/api/official/token", methods=["POST"])
def exchange_device_code():
    """Exchange a device code for SIMKL tokens, adding the server-held secret.

    The browser polls this while the user approves the request on simkl.com.
    SIMKL's pending/denied/expired states come back as 4xx with a JSON body, so
    the upstream status and body are passed through unchanged for the page to
    interpret.
    """
    payload = request.get_json(silent=True) or {}
    device_code = str(payload.get("device_code", "")).strip()

    if not device_code:
        return (
            jsonify({"error": "invalid_request", "error_description": "device_code is required"}),
            400,
        )

    try:
        response = requests.post(
            SIMKL_TOKEN_URL,
            data={
                "grant_type": DEVICE_CODE_GRANT,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "device_code": device_code,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": "Kometa-Utilities/2.0",
            },
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        return jsonify({"error": "upstream_unavailable", "error_description": str(exc)}), 502

    try:
        body = response.json()
    except ValueError:
        return (
            jsonify(
                {
                    "error": "upstream_invalid",
                    "error_description": "SIMKL returned a non-JSON response",
                }
            ),
            502,
        )

    return jsonify(body), response.status_code


@app.route("/logo.svg")
def logo():
    """Serve the SIMKL logo."""
    return send_file(LOGO_PATH, mimetype="image/svg+xml")


@app.route("/api/health", methods=["GET"])
@app.route("/health/live", methods=["GET"])
@app.route("/health/ready", methods=["GET"])
def health():
    """Report service health."""
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)  # nosec B104
