"""
Plex OAuth Flask Application
Coming Soon
"""

from flask import Flask

app = Flask(__name__)


@app.route("/")
def index():
    """Coming soon page."""
    return render_template("index.html", app_name=APP_NAME)


@app.route("/auth/start")
def auth_start():
    """Start the authentication flow by creating a PIN."""
    try:
        # Create a PIN
        response = requests.post(
            f"{PLEX_API_URL}/pins",
            headers=get_plex_headers(),
            params={"strong": "true"},
        )
        response.raise_for_status()
        pin_data = response.json()

        # Store PIN ID in session
        session["pin_id"] = pin_data["id"]
        session["pin_code"] = pin_data["code"]

        # Construct auth URL
        script_name = os.environ.get('ROOT_PATH', '')
        base_url = request.url_root.rstrip("/")
        forward_url = f"{base_url}{script_name}/auth/callback"

        auth_url = (
            f"{PLEX_AUTH_URL}#?"
            f"clientID={get_client_identifier()}&"
            f"code={pin_data['code']}&"
            f"forwardUrl={forward_url}&"
            f"context[device][product]={APP_NAME}"
        )

        # Redirect to Plex auth
        return redirect(auth_url)

    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Plex OAuth - Coming Soon</title>
        <style>
            body {
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
                display: flex;
                justify-content: center;
                align-items: center;
                height: 100vh;
                margin: 0;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
            }
            .container {
                text-align: center;
                padding: 2rem;
                background: rgba(255, 255, 255, 0.1);
                border-radius: 10px;
                backdrop-filter: blur(10px);
            }
            h1 { font-size: 3rem; margin: 0 0 1rem 0; }
            p { font-size: 1.2rem; margin: 0; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🚧 Coming Soon</h1>
            <p>Plex OAuth service is currently under development.</p>
            <p style="margin-top: 2rem;">
                <a href="https://kometa-auth-2cb6c5672416.herokuapp.com/" 
                   style="color: white; text-decoration: underline; font-size: 1.1rem;">
                    Use the existing deployment here →
                </a>
            </p>
        </div>
    </body>
    </html>
    """


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("DEBUG", "False") == "True")
