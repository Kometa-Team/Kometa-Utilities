"""Plex OAuth Flask Application.

A minimal Flask web application for authenticating with Plex and obtaining
access tokens. All Plex API calls are made client-side so that requests
originate from the user's browser, avoiding Plex's server-IP warning.
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
    port = int(os.getenv("PORT", 8080))
    debug = os.getenv("DEBUG", "False").lower() == "true"
    host = os.getenv("HOST", "127.0.0.1")
    app.run(host=host, port=port, debug=debug)
