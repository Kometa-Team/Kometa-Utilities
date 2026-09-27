"""Tests for the SIMKL AUTH V2 token-exchange proxy."""

from unittest.mock import MagicMock, patch

import pytest
import requests
from simkl_oauth.app import DEVICE_CODE_GRANT, SIMKL_TOKEN_URL, app


@pytest.fixture
def client():
    """Return a Flask test client."""
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client


@pytest.mark.parametrize("path", ["/api/health", "/health/live", "/health/ready"])
def test_health_endpoint(client, path) -> None:
    """Health endpoints report ok."""
    response = client.get(path)
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_logo(client) -> None:
    """The logo is served as SVG."""
    response = client.get("/logo.svg")
    assert response.status_code == 200
    assert "svg" in response.headers["Content-Type"]


def _simkl_response(status: int, body: dict) -> MagicMock:
    mock = MagicMock()
    mock.status_code = status
    mock.json.return_value = body
    return mock


def test_exchange_injects_server_credentials(client) -> None:
    """The proxy adds its own client_id and secret to the exchange.

    The page cannot: SIMKL rejects the exchange with invalid_client unless the
    secret is present, and a public static page cannot hold one.
    """
    tokens = {"access_token": "simkl_at_A", "refresh_token": "simkl_rt_B", "expires_in": 604800}
    with patch("simkl_oauth.app.requests.post", return_value=_simkl_response(200, tokens)) as post:
        response = client.post("/api/official/token", json={"device_code": "DEV123"})

    assert response.status_code == 200
    assert response.get_json() == tokens
    sent = post.call_args.kwargs["data"]
    assert sent["grant_type"] == DEVICE_CODE_GRANT
    assert sent["device_code"] == "DEV123"
    assert sent["client_id"] == "test-client-id"
    assert sent["client_secret"] == "test-client-secret"


def test_exchange_ignores_caller_supplied_credentials(client) -> None:
    """A caller cannot make this an open relay for another SIMKL app."""
    with patch("simkl_oauth.app.requests.post", return_value=_simkl_response(200, {})) as post:
        client.post(
            "/api/official/token",
            json={
                "device_code": "DEV123",
                "client_id": "attacker-client",
                "client_secret": "attacker-secret",
            },
        )

    sent = post.call_args.kwargs["data"]
    assert sent["client_id"] == "test-client-id"
    assert sent["client_secret"] == "test-client-secret"


@pytest.mark.parametrize(
    "error", ["authorization_pending", "slow_down", "access_denied", "expired_token"]
)
def test_pending_and_error_states_pass_through(client, error) -> None:
    """SIMKL returns these as 4xx with a JSON body; the page needs both intact."""
    with patch(
        "simkl_oauth.app.requests.post", return_value=_simkl_response(400, {"error": error})
    ):
        response = client.post("/api/official/token", json={"device_code": "DEV123"})

    assert response.status_code == 400
    assert response.get_json() == {"error": error}


def test_missing_device_code_is_rejected(client) -> None:
    """No device code means there is nothing to exchange."""
    response = client.post("/api/official/token", json={})
    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_request"


def test_blank_device_code_is_rejected(client) -> None:
    """Whitespace is not a device code."""
    response = client.post("/api/official/token", json={"device_code": "   "})
    assert response.status_code == 400


def test_no_json_body_is_rejected(client) -> None:
    """A bodyless POST must not raise."""
    response = client.post("/api/official/token")
    assert response.status_code == 400


def test_upstream_network_error(client) -> None:
    """A SIMKL outage surfaces as 502, not a stack trace."""
    with patch("simkl_oauth.app.requests.post", side_effect=requests.RequestException("boom")):
        response = client.post("/api/official/token", json={"device_code": "DEV123"})

    assert response.status_code == 502
    assert response.get_json()["error"] == "upstream_unavailable"


def test_upstream_non_json(client) -> None:
    """An HTML error page from SIMKL must not surface as a 500."""
    mock = MagicMock()
    mock.status_code = 200
    mock.json.side_effect = ValueError("not json")
    with patch("simkl_oauth.app.requests.post", return_value=mock):
        response = client.post("/api/official/token", json={"device_code": "DEV123"})

    assert response.status_code == 502
    assert response.get_json()["error"] == "upstream_invalid"


def test_uses_v2_token_endpoint() -> None:
    """The v1 token URL would stop working when SIMKL retires AUTH V1."""
    assert SIMKL_TOKEN_URL == "https://api.simkl.com/oauth2/token"
