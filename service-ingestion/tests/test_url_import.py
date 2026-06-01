import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from app.url_import import (
    UrlImportError,
    _classify_body,
    _parse_and_validate_url,
    fetch_url_document,
    raise_for_private_or_meta_hosts,
)


class _FakeStreamResponse:
    def __init__(
        self,
        status_code: int,
        *,
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aread(self) -> bytes:
        return b"".join(self._chunks)

    def raise_for_status(self) -> None:
        if self.status_code < 400:
            return
        request = httpx.Request("GET", "https://example.net/resource")
        response = httpx.Response(self.status_code, request=request)
        raise httpx.HTTPStatusError("remote error", request=request, response=response)

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class _FakeAsyncClient:
    def __init__(self, responses: list[_FakeStreamResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def factory(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, method: str, url: str):
        self.calls.append((method, url))
        if not self._responses:
            raise AssertionError(f"unexpected request to {url}")
        return self._responses.pop(0)


def _settings(**overrides):
    values = {
        "url_import_enabled": True,
        "url_import_max_bytes": 1024,
        "url_import_max_redirects": 5,
        "url_import_timeout_seconds": 1.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


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


def test_parse_rejects_invalid_port_with_import_error():
    with pytest.raises(UrlImportError) as exc:
        _parse_and_validate_url("https://example.com:99999/path")
    assert exc.value.status_code == 400
    assert exc.value.detail == "Invalid port"


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


def test_fetch_revalidates_redirect_target_before_following():
    client = _FakeAsyncClient(
        [
            _FakeStreamResponse(
                302,
                headers={"location": "http://127.0.0.1/internal-metadata"},
            )
        ]
    )

    with patch("app.url_import.httpx.AsyncClient", new=client.factory):
        with pytest.raises(UrlImportError) as exc:
            asyncio.run(
                fetch_url_document(
                    "https://93.184.216.34/start",
                    _settings(url_import_max_redirects=3),
                )
            )

    assert exc.value.status_code == 403
    assert client.calls == [("GET", "https://93.184.216.34/start")]


def test_fetch_enforces_streamed_body_limit_before_storage():
    client = _FakeAsyncClient(
        [
            _FakeStreamResponse(
                200,
                headers={"content-type": "text/plain"},
                chunks=[b"1234", b"567"],
            )
        ]
    )

    with patch("app.url_import.httpx.AsyncClient", new=client.factory):
        with pytest.raises(UrlImportError) as exc:
            asyncio.run(
                fetch_url_document(
                    "https://93.184.216.34/document.txt",
                    _settings(url_import_max_bytes=5),
                )
            )

    assert exc.value.status_code == 413
    assert client.calls == [("GET", "https://93.184.216.34/document.txt")]


def test_fetch_maps_remote_http_error_to_bad_gateway():
    client = _FakeAsyncClient([_FakeStreamResponse(404)])

    with patch("app.url_import.httpx.AsyncClient", new=client.factory):
        with pytest.raises(UrlImportError) as exc:
            asyncio.run(
                fetch_url_document("https://93.184.216.34/missing", _settings())
            )

    assert exc.value.status_code == 502
    assert exc.value.detail == "Remote server returned 404"
