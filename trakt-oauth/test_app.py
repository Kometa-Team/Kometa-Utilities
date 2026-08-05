"""Tests for the Trakt OAuth Flask application."""

import importlib
from urllib.parse import parse_qs, urlparse

import pytest

app_module = importlib.import_module("trakt_oauth.app")
app = app_module.app


@pytest.fixture()
def client():
    app.config["TESTING"] = True
    app_module.pending_authorizations.clear()
    with app.test_client() as test_client:
        yield test_client
    app_module.pending_authorizations.clear()


@pytest.mark.parametrize("path", ["/api/health", "/health/live", "/health/ready"])
def test_health_endpoint(client, path) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_start_authorization_requires_credentials(client) -> None:
    response = client.post("/api/start", json={"client_id": "client"})
    assert response.status_code == 400


def test_start_authorization_uses_callback_and_one_time_state(client) -> None:
    response = client.post(
        "/api/start",
        json={"client_id": "client", "client_secret": "secret"},
    )

    assert response.status_code == 200
    query = parse_qs(urlparse(response.get_json()["authorization_url"]).query)
    assert query["client_id"] == ["client"]
    assert query["redirect_uri"] == [app_module.TRAKT_REDIRECT_URI]
    assert len(query["state"][0]) >= 32
    assert app_module.pending_authorizations[query["state"][0]]["client_secret"] == "secret"


def test_callback_exchanges_code_and_rejects_state_replay(client, monkeypatch) -> None:
    start_response = client.post(
        "/api/start",
        json={"client_id": "client", "client_secret": "secret"},
    )
    state = parse_qs(urlparse(start_response.get_json()["authorization_url"]).query)["state"][0]
    exchange_arguments = []

    def fake_exchange(client_id, client_secret, code, redirect_uri):
        exchange_arguments.append((client_id, client_secret, code, redirect_uri))
        return {
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_in": 123,
            "token_type": "Bearer",
            "created_at": 456,
        }

    monkeypatch.setattr(app_module, "exchange_code_for_token", fake_exchange)

    response = client.get(f"/callback?code=code&state={state}")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert b"access" in response.data
    assert exchange_arguments == [("client", "secret", "code", app_module.TRAKT_REDIRECT_URI)]

    replay_response = client.get(f"/callback?code=code&state={state}")
    assert replay_response.status_code == 400
    assert b"already used" in replay_response.data


def test_callback_rejects_unknown_state(client) -> None:
    response = client.get("/callback?code=code&state=unknown")
    assert response.status_code == 400
    assert b"invalid, expired, or already used" in response.data


def test_official_start_returns_503_when_unconfigured(client, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "CLIENT_ID", "")
    monkeypatch.setattr(app_module, "CLIENT_SECRET", "")
    response = client.post("/api/official/start", json={})
    assert response.status_code == 503


def test_official_start_builds_url_with_server_credentials(client, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "CLIENT_ID", "server-client")
    monkeypatch.setattr(app_module, "CLIENT_SECRET", "server-secret")
    response = client.post("/api/official/start", json={})

    assert response.status_code == 200
    query = parse_qs(urlparse(response.get_json()["authorization_url"]).query)
    assert query["client_id"] == ["server-client"]
    assert query["redirect_uri"] == [app_module.TRAKT_REDIRECT_URI]
    assert len(query["state"][0]) >= 32
    pending = app_module.pending_authorizations[query["state"][0]]
    assert pending["client_id"] == "server-client"
    assert pending["client_secret"] == "server-secret"


def test_official_exchange_uses_server_credentials_and_is_single_use(client, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "CLIENT_ID", "server-client")
    monkeypatch.setattr(app_module, "CLIENT_SECRET", "server-secret")
    start_response = client.post("/api/official/start", json={})
    state = parse_qs(urlparse(start_response.get_json()["authorization_url"]).query)["state"][0]
    exchange_arguments = []

    def fake_exchange(client_id, client_secret, code, redirect_uri):
        exchange_arguments.append((client_id, client_secret, code, redirect_uri))
        return {
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_in": 123,
            "token_type": "Bearer",
            "created_at": 456,
        }

    monkeypatch.setattr(app_module, "exchange_code_for_token", fake_exchange)

    response = client.post("/api/official/exchange", json={"code": "code", "state": state})
    assert response.status_code == 200
    data = response.get_json()
    assert data["success"] is True
    assert data["client_id"] == "server-client"
    assert data["client_secret"] == "server-secret"
    assert data["access_token"] == "access"
    assert exchange_arguments == [
        ("server-client", "server-secret", "code", app_module.TRAKT_REDIRECT_URI)
    ]

    replay_response = client.post("/api/official/exchange", json={"code": "code", "state": state})
    assert replay_response.status_code == 400


def test_official_exchange_requires_code_and_state(client) -> None:
    response = client.post("/api/official/exchange", json={"code": "code"})
    assert response.status_code == 400
