import asyncio
import socket
from types import SimpleNamespace

import httpx
import pytest

from app.url_import import (
    UrlImportError,
    _classify_body,
    fetch_url_document,
    _parse_and_validate_url,
    raise_for_private_or_meta_hosts,
)


def _settings(**overrides):
    defaults = {
        "url_import_enabled": True,
        "url_import_max_bytes": 1024,
        "url_import_max_redirects": 5,
        "url_import_timeout_seconds": 1.0,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class _FakeResponse:
    def __init__(self, status_code=200, headers=None, chunks=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = list(chunks or [b"hello"])

    async def aread(self):
        return b"".join(self._chunks)

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://example.com/doc")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("remote error", request=request, response=response)


class _FakeStream:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _install_fake_client(monkeypatch, responses):
    requested_urls = []
    queued = list(responses)

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url):
            requested_urls.append(url)
            return _FakeStream(queued.pop(0))

    monkeypatch.setattr("app.url_import.httpx.AsyncClient", _FakeAsyncClient)
    return requested_urls


def _allow_public_dns(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("93.184.216.34", 443),
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


def test_parse_rejects_invalid_port():
    with pytest.raises(UrlImportError) as exc:
        _parse_and_validate_url("https://example.com:99999/doc")
    assert exc.value.status_code == 400
    assert "port" in exc.value.detail.lower()


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


def test_fetch_url_document_revalidates_redirect_target_before_request(monkeypatch):
    _allow_public_dns(monkeypatch)
    requested_urls = _install_fake_client(
        monkeypatch,
        [
            _FakeResponse(
                status_code=302,
                headers={"location": "http://169.254.169.254/latest/meta-data"},
                chunks=[b""],
            )
        ],
    )

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(
            fetch_url_document("https://example.com/start", _settings())
        )

    assert exc.value.status_code == 403
    assert requested_urls == ["https://example.com/start"]


def test_fetch_url_document_enforces_streamed_body_limit(monkeypatch):
    _allow_public_dns(monkeypatch)
    _install_fake_client(
        monkeypatch,
        [
            _FakeResponse(
                headers={"content-type": "text/plain"},
                chunks=[b"12345", b"67890"],
            )
        ],
    )

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(
            fetch_url_document(
                "https://example.com/large.txt",
                _settings(url_import_max_bytes=6),
            )
        )

    assert exc.value.status_code == 413


def test_fetch_url_document_preserves_markdown_extension(monkeypatch):
    _allow_public_dns(monkeypatch)
    _install_fake_client(
        monkeypatch,
        [
            _FakeResponse(
                headers={"content-type": "text/markdown; charset=utf-8"},
                chunks=[b"# Title\n\nbody"],
            )
        ],
    )

    body, ext = asyncio.run(
        fetch_url_document("https://example.com/readme.md", _settings())
    )

    assert body == b"# Title\n\nbody"
    assert ext == ".md"


def test_fetch_url_document_maps_remote_http_errors(monkeypatch):
    _allow_public_dns(monkeypatch)
    _install_fake_client(monkeypatch, [_FakeResponse(status_code=404)])

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(fetch_url_document("https://example.com/missing", _settings()))

    assert exc.value.status_code == 502
    assert "404" in exc.value.detail
