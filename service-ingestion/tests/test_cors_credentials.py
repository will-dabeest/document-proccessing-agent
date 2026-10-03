"""Credentialed CORS is limited to the Vite dev origins."""

from fastapi.testclient import TestClient

_VITE_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")
_REJECTED_ORIGINS = (
    "https://evil.example",
    "https://localhost:5173",
    "http://localhost:5174",
)


def _headers(response):
    return {k.lower(): v for k, v in response.headers.items()}


def test_cors_credentials_are_granted_only_to_vite_origins():
    """allow_credentials must echo the allowlisted origin, never '*'."""
    from app.main import app

    client = TestClient(app)
    for origin in _VITE_ORIGINS:
        response = client.get("/health", headers={"Origin": origin})
        assert response.status_code == 200
        headers = _headers(response)
        assert headers.get("access-control-allow-origin") == origin
        assert headers.get("access-control-allow-credentials") == "true"
        assert headers.get("access-control-allow-origin") != "*"

        preflight = client.options(
            "/import-url",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert preflight.status_code in (200, 204)
        preflight_headers = _headers(preflight)
        assert preflight_headers.get("access-control-allow-origin") == origin
        assert preflight_headers.get("access-control-allow-credentials") == "true"
        assert preflight_headers.get("access-control-allow-origin") != "*"


def test_cors_credentials_are_withheld_from_other_origins():
    """A foreign or lookalike Origin must not be echoed, so the browser will not expose the response."""
    from app.main import app

    client = TestClient(app)
    for origin in _REJECTED_ORIGINS:
        response = client.get("/health", headers={"Origin": origin})
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        headers = _headers(response)
        assert "access-control-allow-origin" not in headers

        preflight = client.options(
            "/import-url",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert preflight.status_code == 400
        preflight_headers = _headers(preflight)
        assert "access-control-allow-origin" not in preflight_headers
