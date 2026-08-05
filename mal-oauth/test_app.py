import importlib
from urllib.parse import parse_qs, urlparse

import pytest

app_module = importlib.import_module("mal_oauth.app")
app = app_module.app


@pytest.fixture()
def client():
    """Flask test client for the MAL OAuth app."""
    app_module.pending_authorizations.clear()
    with app.test_client() as client:
        yield client
    app_module.pending_authorizations.clear()


@pytest.mark.parametrize("path", ["/api/health", "/health/live", "/health/ready"])
def test_health_endpoint(client, path) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_index_page(client) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert b'/logo.svg' in response.data


def test_logo(client) -> None:
    response = client.get("/logo.svg")
    assert response.status_code == 200
    assert response.mimetype == "image/svg+xml"
    assert response.headers["Cache-Control"] == "public, max-age=86400"
    assert response.data.lstrip().startswith(b"<!-- License:")


def test_official_start_returns_503_when_unconfigured(client, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "CLIENT_ID", "")
    response = client.post("/api/official/start", json={})
    assert response.status_code == 503


def test_official_start_builds_pkce_url_with_server_credentials(client, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "CLIENT_ID", "server-client")
    monkeypatch.setattr(app_module, "CLIENT_SECRET", "server-secret")
    response = client.post("/api/official/start", json={})

    assert response.status_code == 200
    query = parse_qs(urlparse(response.get_json()["authorization_url"]).query)
    assert query["client_id"] == ["server-client"]
    assert query["redirect_uri"] == [app_module.MAL_REDIRECT_URI]
    assert len(query["state"][0]) >= 32
    assert query["code_challenge_method"] == ["plain"]
    pending = app_module.pending_authorizations[query["state"][0]]
    assert pending["client_id"] == "server-client"
    assert pending["client_secret"] == "server-secret"
    assert pending["code_verifier"] == query["code_challenge"][0]


def test_official_exchange_uses_server_credentials_and_never_returns_secret(client, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "CLIENT_ID", "server-client")
    monkeypatch.setattr(app_module, "CLIENT_SECRET", "server-secret")
    start_response = client.post("/api/official/start", json={})
    query = parse_qs(urlparse(start_response.get_json()["authorization_url"]).query)
    state = query["state"][0]
    exchange_arguments = []

    def fake_exchange(client_id, client_secret, code, code_verifier, redirect_uri):
        exchange_arguments.append((client_id, client_secret, code, code_verifier, redirect_uri))
        return {
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_in": 123,
            "token_type": "Bearer",
        }

    monkeypatch.setattr(app_module, "exchange_code_for_token", fake_exchange)

    response = client.post(
        "/api/official/exchange", json={"code": "code", "state": state}
    )
    assert response.status_code == 200
    data = response.get_json()
    assert data["success"] is True
    assert data["client_id"] == "server-client"
    assert "client_secret" not in data
    assert data["access_token"] == "access"
    assert exchange_arguments == [
        (
            "server-client",
            "server-secret",
            "code",
            query["code_challenge"][0],
            app_module.MAL_REDIRECT_URI,
        )
    ]

    replay_response = client.post(
        "/api/official/exchange", json={"code": "code", "state": state}
    )
    assert replay_response.status_code == 400


def test_official_exchange_requires_code_and_state(client) -> None:
    response = client.post("/api/official/exchange", json={"code": "code"})
    assert response.status_code == 400


def test_exchange_code_for_token_sends_redirect_uri(monkeypatch) -> None:
    captured = {}

    class FakeResponse:
        status_code = 200
        text = "ok"

        def raise_for_status(self) -> None:
            pass

        def json(self):
            return {"access_token": "access"}

    def fake_post(url, data=None, timeout=None):
        captured["url"] = url
        captured["data"] = dict(data)
        return FakeResponse()

    monkeypatch.setattr(app_module.requests, "post", fake_post)

    result = app_module.exchange_code_for_token(
        "client", "secret", "code", "verifier", "https://utilities.kometa.wiki/mal-oauth/callback"
    )

    assert result == {"access_token": "access"}
    assert captured["url"] == "https://myanimelist.net/v1/oauth2/token"
    assert captured["data"] == {
        "client_id": "client",
        "client_secret": "secret",
        "code": "code",
        "code_verifier": "verifier",
        "grant_type": "authorization_code",
        "redirect_uri": "https://utilities.kometa.wiki/mal-oauth/callback",
    }


def test_exchange_code_for_token_omits_secret_when_empty(monkeypatch) -> None:
    captured = {}

    class FakeResponse:
        status_code = 200
        text = "ok"

        def raise_for_status(self) -> None:
            pass

        def json(self):
            return {"access_token": "access"}

    def fake_post(url, data=None, timeout=None):
        captured["data"] = dict(data)
        return FakeResponse()

    monkeypatch.setattr(app_module.requests, "post", fake_post)

    app_module.exchange_code_for_token("client", "", "code", "verifier", "https://cb/")

    assert "client_secret" not in captured["data"]
    assert captured["data"]["redirect_uri"] == "https://cb/"


def test_legacy_exchange_code_derives_redirect_uri_from_localhost_url(client, monkeypatch) -> None:
    exchange_arguments = []

    def fake_exchange(client_id, client_secret, code, code_verifier, redirect_uri):
        exchange_arguments.append(redirect_uri)
        return {
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_in": 123,
            "token_type": "Bearer",
        }

    monkeypatch.setattr(app_module, "exchange_code_for_token", fake_exchange)

    response = client.post(
        "/api/exchange-code",
        json={
            "client_id": "byo-client",
            "client_secret": "byo-secret",
            "localhost_url": "http://localhost:8765/?code=abc123&state=xyz",
            "code_verifier": "verifier",
        },
    )

    assert response.status_code == 200
    assert exchange_arguments == ["http://localhost:8765/"]
