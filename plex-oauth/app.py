"""
Plex OAuth Flask Application
Coming Soon
"""

from flask import Flask

app = Flask(__name__)


@app.route("/")
def index():
    """Coming soon page."""
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
                <a href="https://plex-oauth-0b43dcf08594.herokuapp.com/" 
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
