# Plex-OAuth

A minimal Flask web application for authenticating with Plex and obtaining access tokens for use with [Kometa](https://github.com/Kometa-Team/Kometa).

## Overview

This app provides a simple web interface for users to authenticate with their Plex account and receive an access token that can be used with Kometa or other Plex-based applications.

## Features

- Simple, user-friendly web interface
- OAuth 2.0 PIN-based authentication flow
- Secure token generation
- One-click token copy to clipboard
- Ready for Heroku deployment

## Quick Start

### Option 1: Dev Container (Recommended)

The easiest way to get started is using VS Code Dev Containers:

1. **Prerequisites**:
   - Install [Docker Desktop](https://www.docker.com/products/docker-desktop)
   - Install [VS Code](https://code.visualstudio.com/)
   - Install the [Dev Containers extension](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers)

2. **Open in container**:
   - Open this project in VS Code
   - Press `F1` and select `Dev Containers: Reopen in Container`
   - Wait for setup to complete (automatically installs everything)

3. **Run the app**:
   ```bash
   uv run python app.py
   ```

4. **Open your browser** to `http://localhost:8080`

See [.devcontainer/README.md](.devcontainer/README.md) for more details.

### Option 2: Local Development

1. **Install uv** (if not already installed):
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. **Clone the repository**:
   ```bash
   git clone https://github.com/Kometa-Team/Plex-OAuth.git
   cd Plex-OAuth
   ```

3. **Install dependencies**:
   ```bash
   uv sync
   ```

4. **Create environment file**:
   ```bash
   cp .env.example .env
   # Edit .env with your settings
   ```

5. **Run the application**:
   ```bash
   uv run python app.py
   ```

6. **Open your browser** to `http://localhost:8080`

### Using the App

1. Click "Authenticate with Plex"
2. Sign in with your Plex account
3. Authorize the application
4. Copy the displayed access token
5. Use the token in your Kometa configuration

## Deployment to Heroku

### Prerequisites

- [Heroku CLI](https://devcenter.heroku.com/articles/heroku-cli) installed
- A Heroku account

### Deployment Steps

1. **Create a new Heroku app**:
   ```bash
   heroku create your-app-name
   ```

2. **Set environment variables**:
   ```bash
   heroku config:set SECRET_KEY=$(python -c 'import os; print(os.urandom(24).hex())')
   heroku config:set APP_NAME="Kometa Plex Auth"
   ```

3. **Deploy the application**:
   ```bash
   git push heroku main
   ```

4. **Open your app**:
   ```bash
   heroku open
   ```

### Environment Variables

Configure these in Heroku's dashboard or via the CLI:

- `SECRET_KEY` - Flask secret key (required for sessions)
- `APP_NAME` - Name displayed in the app and to Plex (default: "Kometa Plex Auth")
- `APP_VERSION` - Version string (default: "1.0")
- `DEBUG` - Enable debug mode (default: "False", use "True" only in development)

## Configuration

Copy `.env.example` to `.env` for local development:

```bash
cp .env.example .env
```

Edit `.env` with your preferred settings.

## Project Structure

```
Plex-OAuth/
├── app.py                 # Main Flask application
├── templates/             # HTML templates
│   ├── base.html         # Base template with styling
│   ├── index.html        # Home page
│   ├── success.html      # Success page with token
│   └── error.html        # Error page
├── pyproject.toml        # Project dependencies (uv)
├── Procfile              # Heroku process file
├── runtime.txt           # Python version for Heroku
└── README.md             # This file
```

## Security Notes

- The app generates a unique client identifier for each user session
- Tokens are displayed only once after successful authentication
- Always use HTTPS in production (Heroku provides this automatically)
- Keep your `SECRET_KEY` secure and never commit it to version control
- The generated token provides full access to the user's Plex account - handle with care

## Development

### Running Tests

```bash
uv run pytest
```

### Adding Dependencies

```bash
uv add package-name
```

## Troubleshooting

### "No authentication session found" error
- This happens if cookies are disabled or cleared between steps
- Ensure cookies are enabled in your browser
- Try the authentication flow again

### Token not appearing
- Check that you completed the Plex authentication
- Verify the app has network access to plex.tv
- Check Heroku logs: `heroku logs --tail`

## Links

- [Plex OAuth Documentation](https://forums.plex.tv/t/authenticating-with-plex/609370)
- [Kometa Documentation](https://kometa.wiki/)
- [uv Documentation](https://github.com/astral-sh/uv)

## License

MIT License - see [LICENSE](LICENSE) file for details

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.
