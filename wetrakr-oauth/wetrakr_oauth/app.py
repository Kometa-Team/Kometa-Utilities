"""WeTrakr OAuth Flask Application.

A minimal Flask web application serving the WeTrakr device-code auth page
for Kometa. All WeTrakr API calls happen through the same-origin Caddy
proxy at /wetrakr-oauth/api/device/* (see the Caddyfile), never through
this process — api.wetrakr.com sends no CORS headers on /oauth/device/*, so
a direct browser fetch() to api.wetrakr.com would fail, and Caddy forwards
the request untouched instead. No client_secret is used anywhere in this
flow, so nothing sensitive ever reaches this server either way.
"""

import os

from flask import Flask, jsonify, render_template

app = Flask(__name__, template_folder="../templates")
app.secret_key = os.getenv("SECRET_KEY", "dev-key-change-in-production")


@app.route("/")
def index():
    """Render the main page."""
    return render_template("index.html")


@app.route("/api/health", methods=["GET"])
@app.route("/health/live", methods=["GET"])
@app.route("/health/ready", methods=["GET"])
def health():
    """Health check endpoint."""
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    debug = os.getenv("DEBUG", "False").lower() == "true"
    host = os.getenv("HOST", "127.0.0.1")
    app.run(host=host, port=port, debug=debug)
