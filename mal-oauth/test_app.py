import pytest
from mal_oauth.app import app


@pytest.fixture()
def client():
    """Flask test client for the MAL OAuth app."""
    with app.test_client() as client:
        yield client


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
