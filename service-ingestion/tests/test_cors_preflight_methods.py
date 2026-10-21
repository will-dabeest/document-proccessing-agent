"""Credentialed browser writes from Vite need POST and Content-Type on preflight."""

from fastapi.testclient import TestClient

_VITE_ORIGIN = "http://127.0.0.1:5173"
_WRITE_PATHS = ("/upload", "/import-url", "/ask")


def _headers(response):
    return {k.lower(): v for k, v in response.headers.items()}


def test_cors_preflight_allows_post_and_content_type_from_vite():
    from app.main import app

    client = TestClient(app)
    for path in _WRITE_PATHS:
        response = client.options(
            path,
            headers={
                "Origin": _VITE_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert response.status_code in (200, 204), path
        headers = _headers(response)
        methods = {
            part.strip().upper()
            for part in headers.get("access-control-allow-methods", "").split(",")
            if part.strip()
        }
        allowed_headers = {
            part.strip().lower()
            for part in headers.get("access-control-allow-headers", "").split(",")
            if part.strip()
        }
        assert "POST" in methods, path
        assert "content-type" in allowed_headers, path
