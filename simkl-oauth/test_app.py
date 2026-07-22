"""Tests for the SIMKL OAuth Flask application."""

from unittest.mock import MagicMock, patch

import pytest
import requests as req  # type: ignore[import-untyped]


@pytest.fixture()
def client():
    """Flask test client."""
    from simkl_oauth.app import app

    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.mark.parametrize("path", ["/api/health", "/health/live", "/health/ready"])
def test_health_endpoint(client, path) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_index_page_renders(client) -> None:
    response = client.get("/")
    assert response.status_code == 200


def test_index_page_contains_auth_url(client) -> None:
    response = client.get("/")
    html = response.data.decode()
    assert "https://simkl.com/oauth/authorize" in html
    assert "test-client-id" in html
    assert "localhost" in html


def test_exchange_code_for_token_success() -> None:
    from simkl_oauth.app import exchange_code_for_token

    mock_response = MagicMock()
    mock_response.json.return_value = {"access_token": "tok_abc123"}
    mock_response.raise_for_status.return_value = None

    with patch("simkl_oauth.app.requests.post", return_value=mock_response):
        result = exchange_code_for_token("auth-code-xyz")

    assert result == {"access_token": "tok_abc123"}


def test_exchange_code_for_token_http_error() -> None:
    from simkl_oauth.app import exchange_code_for_token

    mock_response = MagicMock()
    mock_response.text = '{"error":"invalid_grant"}'
    http_error = req.exceptions.HTTPError(response=mock_response)

    with patch("simkl_oauth.app.requests.post", side_effect=http_error):
        result = exchange_code_for_token("bad-code")

    assert result is not None
    assert "error" in result
    assert "invalid_grant" in result["error"]


def test_exchange_code_for_token_network_error() -> None:
    from simkl_oauth.app import exchange_code_for_token

    with patch("simkl_oauth.app.requests.post", side_effect=ConnectionError("timeout")):
        result = exchange_code_for_token("any-code")

    assert result is None


def test_callback_success(client) -> None:
    mock_response = MagicMock()
    mock_response.json.return_value = {"access_token": "tok_abc123"}
    mock_response.raise_for_status.return_value = None

    with patch("simkl_oauth.app.requests.post", return_value=mock_response):
        response = client.get("/callback?code=auth-code-xyz")

    assert response.status_code == 200
    html = response.data.decode()
    assert "tok_abc123" in html


def test_callback_error_param(client) -> None:
    response = client.get("/callback?error=access_denied&error_description=User+denied+access")
    assert response.status_code == 200
    html = response.data.decode()
    assert "User denied access" in html


def test_callback_error_param_no_description(client) -> None:
    response = client.get("/callback?error=access_denied")
    assert response.status_code == 200
    html = response.data.decode()
    assert "access_denied" in html


def test_callback_no_code(client) -> None:
    response = client.get("/callback")
    assert response.status_code == 200
    html = response.data.decode()
    assert "Error" in html or "error" in html


def test_callback_exchange_fails(client) -> None:
    with patch("simkl_oauth.app.requests.post", side_effect=ConnectionError("timeout")):
        response = client.get("/callback?code=any-code")
    assert response.status_code == 200
    html = response.data.decode()
    assert "Error" in html or "error" in html


def test_callback_exchange_http_error(client) -> None:
    mock_response = MagicMock()
    mock_response.text = '{"error":"invalid_grant"}'
    http_error = req.exceptions.HTTPError(response=mock_response)

    with patch("simkl_oauth.app.requests.post", side_effect=http_error):
        response = client.get("/callback?code=bad-code")

    assert response.status_code == 200
    html = response.data.decode()
    assert "Error" in html or "error" in html


def test_callback_missing_access_token(client) -> None:
    mock_response = MagicMock()
    mock_response.json.return_value = {"token_type": "Bearer"}  # no access_token
    mock_response.raise_for_status.return_value = None

    with patch("simkl_oauth.app.requests.post", return_value=mock_response):
        response = client.get("/callback?code=any-code")

    assert response.status_code == 200
    html = response.data.decode()
    assert "Error" in html or "error" in html
