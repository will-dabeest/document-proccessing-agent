import asyncio
import socket
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


class _FakeResponse:
    def __init__(self, status_code, *, headers=None, chunks=()):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def aread(self):
        return b"".join(self._chunks)

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    def raise_for_status(self):
        if self.status_code < 400:
            return
        request = httpx.Request("GET", "https://example.com/document")
        response = httpx.Response(self.status_code, request=request)
        raise httpx.HTTPStatusError(
            "Remote request failed", request=request, response=response
        )


class _FakeAsyncClient:
    def __init__(self, responses):
        self._responses = iter(responses)
        self.requested_urls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def stream(self, method, url):
        assert method == "GET"
        self.requested_urls.append(url)
        return next(self._responses)


def _settings(**overrides):
    values = {
        "url_import_enabled": True,
        "url_import_timeout_seconds": 5.0,
        "url_import_max_redirects": 3,
        "url_import_max_bytes": 1024,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _public_dns():
    return patch(
        "app.url_import.socket.getaddrinfo",
        return_value=[
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 0),
            )
        ],
    )


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


def test_fetch_revalidates_redirect_target_before_requesting_it():
    client = _FakeAsyncClient(
        [_FakeResponse(302, headers={"location": "http://127.0.0.1/private"})]
    )

    with patch("app.url_import.httpx.AsyncClient", return_value=client), _public_dns():
        with pytest.raises(UrlImportError) as exc:
            asyncio.run(fetch_url_document("https://example.com/start", _settings()))

    assert exc.value.status_code == 403
    assert client.requested_urls == ["https://example.com/start"]


def test_fetch_enforces_streamed_body_limit():
    client = _FakeAsyncClient(
        [_FakeResponse(200, headers={"content-type": "text/plain"}, chunks=[b"abc", b"def"])]
    )

    with patch("app.url_import.httpx.AsyncClient", return_value=client), _public_dns():
        with pytest.raises(UrlImportError) as exc:
            asyncio.run(
                fetch_url_document(
                    "https://example.com/document", _settings(url_import_max_bytes=5)
                )
            )

    assert exc.value.status_code == 413
    assert exc.value.detail == "Response body exceeds configured limit"


def test_fetch_maps_remote_http_error_to_bad_gateway():
    client = _FakeAsyncClient([_FakeResponse(404)])

    with patch("app.url_import.httpx.AsyncClient", return_value=client), _public_dns():
        with pytest.raises(UrlImportError) as exc:
            asyncio.run(fetch_url_document("https://example.com/missing", _settings()))

    assert exc.value.status_code == 502
    assert exc.value.detail == "Remote server returned 404"


def test_fetch_preserves_markdown_extension_and_body():
    client = _FakeAsyncClient(
        [
            _FakeResponse(
                200,
                headers={"content-type": "text/markdown; charset=utf-8"},
                chunks=[b"# Heading\n", b"Document body"],
            )
        ]
    )

    with patch("app.url_import.httpx.AsyncClient", return_value=client), _public_dns():
        body, extension = asyncio.run(
            fetch_url_document("https://example.com/readme", _settings())
        )

    assert body == b"# Heading\nDocument body"
    assert extension == ".md"
