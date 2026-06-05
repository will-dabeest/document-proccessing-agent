import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from app.url_import import (
    UrlImportError,
    _classify_body,
    _parse_and_validate_url,
    raise_for_private_or_meta_hosts,
)


class FakeStreamResponse:
    def __init__(
        self,
        status_code: int,
        *,
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
        url: str = "https://example.com/doc",
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks or []
        self._request = httpx.Request("GET", url)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def aread(self) -> bytes:
        return b"".join(self._chunks)

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            response = httpx.Response(self.status_code, request=self._request)
            raise httpx.HTTPStatusError("remote error", request=self._request, response=response)


class FakeAsyncClient:
    routes: dict[str, FakeStreamResponse] = {}
    requests: list[tuple[str, str]] = []

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def stream(self, method: str, url: str):
        self.requests.append((method, url))
        return self.routes[url]


def _url_import_settings(**overrides):
    defaults = {
        "url_import_enabled": True,
        "url_import_timeout_seconds": 1.0,
        "url_import_max_redirects": 3,
        "url_import_max_bytes": 1024,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _public_dns_result(*_args, **_kwargs):
    return [(None, None, None, None, ("93.184.216.34", 443))]


def test_parse_rejects_invalid_port():
    with pytest.raises(UrlImportError) as exc:
        _parse_and_validate_url("https://example.com:99999/doc")

    assert exc.value.status_code == 400
    assert exc.value.detail == "Invalid port"


def test_parse_rejects_file_scheme():
    with pytest.raises(UrlImportError) as exc:
        _parse_and_validate_url("file:///etc/passwd")
    assert exc.value.status_code == 400


def test_parse_rejects_empty_url():
    with pytest.raises(UrlImportError) as exc:
        _parse_and_validate_url("   ")
    assert exc.value.status_code == 400


def test_parse_accepts_https():
    raw, host, port = _parse_and_validate_url("https://example.com/path?q=1")
    assert host == "example.com"
    assert "example.com" in raw
    assert port is None


def test_raise_for_private_blocks_loopback():
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts("127.0.0.1")
    assert exc.value.status_code == 403


def test_raise_for_private_blocks_hostname_localhost():
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts("localhost")
    assert exc.value.status_code == 403


def test_classify_pdf_magic_overrides_octet_stream():
    assert _classify_body("application/octet-stream", b"%PDF-1.4\n1 0 obj") == "pdf"


def test_classify_html():
    assert _classify_body("text/html; charset=utf-8", b"<html></html>") == "html"


def test_classify_unknown_binary():
    assert _classify_body("application/zip", b"PK\x03\x04") == "unknown_binary"


def test_html_to_plain_text_trafilatura_smoke():
    from app.url_import import _html_to_plain_text

    raw = b"<html><body><article><p>UniqueMarkerAlpha beta</p></article></body></html>"
    out = _html_to_plain_text(raw)
    assert b"UniqueMarkerAlpha" in out


def test_fetch_revalidates_redirect_target_before_next_request():
    from app.url_import import fetch_url_document

    FakeAsyncClient.requests = []
    FakeAsyncClient.routes = {
        "https://example.com/start": FakeStreamResponse(
            302,
            headers={"location": "http://127.0.0.1/latest/meta-data"},
            url="https://example.com/start",
        )
    }

    async def run():
        with patch("app.url_import.httpx.AsyncClient", FakeAsyncClient), patch(
            "app.url_import.socket.getaddrinfo", side_effect=_public_dns_result
        ):
            with pytest.raises(UrlImportError) as exc:
                await fetch_url_document(
                    "https://example.com/start", _url_import_settings()
                )
        return exc.value

    err = asyncio.run(run())

    assert err.status_code == 403
    assert FakeAsyncClient.requests == [("GET", "https://example.com/start")]


def test_fetch_enforces_streamed_body_size_limit():
    from app.url_import import fetch_url_document

    FakeAsyncClient.requests = []
    FakeAsyncClient.routes = {
        "https://example.com/large.txt": FakeStreamResponse(
            200,
            headers={"content-type": "text/plain"},
            chunks=[b"1234", b"56"],
            url="https://example.com/large.txt",
        )
    }

    async def run():
        with patch("app.url_import.httpx.AsyncClient", FakeAsyncClient), patch(
            "app.url_import.socket.getaddrinfo", side_effect=_public_dns_result
        ):
            with pytest.raises(UrlImportError) as exc:
                await fetch_url_document(
                    "https://example.com/large.txt",
                    _url_import_settings(url_import_max_bytes=5),
                )
        return exc.value

    err = asyncio.run(run())

    assert err.status_code == 413
    assert err.detail == "Response body exceeds configured limit"


def test_fetch_maps_remote_http_status_to_bad_gateway():
    from app.url_import import fetch_url_document

    FakeAsyncClient.requests = []
    FakeAsyncClient.routes = {
        "https://example.com/missing": FakeStreamResponse(
            404,
            headers={"content-type": "text/plain"},
            chunks=[b"not found"],
            url="https://example.com/missing",
        )
    }

    async def run():
        with patch("app.url_import.httpx.AsyncClient", FakeAsyncClient), patch(
            "app.url_import.socket.getaddrinfo", side_effect=_public_dns_result
        ):
            with pytest.raises(UrlImportError) as exc:
                await fetch_url_document(
                    "https://example.com/missing", _url_import_settings()
                )
        return exc.value

    err = asyncio.run(run())

    assert err.status_code == 502
    assert err.detail == "Remote server returned 404"


def test_fetch_preserves_markdown_extension():
    from app.url_import import fetch_url_document

    FakeAsyncClient.requests = []
    FakeAsyncClient.routes = {
        "https://example.com/readme": FakeStreamResponse(
            200,
            headers={"content-type": "text/markdown; charset=utf-8"},
            chunks=[b"# Title\n\nBody"],
            url="https://example.com/readme",
        )
    }

    async def run():
        with patch("app.url_import.httpx.AsyncClient", FakeAsyncClient), patch(
            "app.url_import.socket.getaddrinfo", side_effect=_public_dns_result
        ):
            return await fetch_url_document(
                "https://example.com/readme", _url_import_settings()
            )

    body, ext = asyncio.run(run())

    assert body == b"# Title\n\nBody"
    assert ext == ".md"
