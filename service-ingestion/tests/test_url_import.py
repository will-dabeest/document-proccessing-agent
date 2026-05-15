import asyncio
import socket
from types import SimpleNamespace

import httpx
import pytest

from app.url_import import (
    UrlImportError,
    _classify_body,
    _parse_and_validate_url,
    fetch_url_document,
    raise_for_private_or_meta_hosts,
)


class _FakeStream:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *_exc):
        return False


class _FakeAsyncClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def stream(self, method, url):
        self.requests.append((method, url))
        if not self._responses:
            raise AssertionError("No fake response queued")
        return _FakeStream(self._responses.pop(0))


def _response(status_code, *, url, headers=None, content=b""):
    return httpx.Response(
        status_code,
        headers=headers or {},
        content=content,
        request=httpx.Request("GET", url),
    )


def _settings(**overrides):
    values = {
        "url_import_enabled": True,
        "url_import_max_bytes": 1024,
        "url_import_max_redirects": 5,
        "url_import_timeout_seconds": 1.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _install_async_client(monkeypatch, responses):
    client = _FakeAsyncClient(responses)
    monkeypatch.setattr("app.url_import.httpx.AsyncClient", lambda **_kwargs: client)
    return client


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


def test_fetch_url_document_blocks_redirect_to_private_host(monkeypatch):
    client = _install_async_client(
        monkeypatch,
        [
            _response(
                302,
                url="https://example.com/start",
                headers={"location": "http://127.0.0.1/admin"},
            )
        ],
    )

    def fake_getaddrinfo(host, *_args, **_kwargs):
        assert host == "example.com"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr("app.url_import.socket.getaddrinfo", fake_getaddrinfo)

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(fetch_url_document("https://example.com/start", _settings()))

    assert exc.value.status_code == 403
    assert client.requests == [("GET", "https://example.com/start")]


def test_fetch_url_document_enforces_max_body_bytes(monkeypatch):
    client = _install_async_client(
        monkeypatch,
        [
            _response(
                200,
                url="https://example.com/big.txt",
                headers={"content-type": "text/plain"},
                content=b"abcdef",
            )
        ],
    )
    monkeypatch.setattr(
        "app.url_import.raise_for_private_or_meta_hosts",
        lambda _host: None,
    )

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(
            fetch_url_document(
                "https://example.com/big.txt",
                _settings(url_import_max_bytes=3),
            )
        )

    assert exc.value.status_code == 413
    assert client.requests == [("GET", "https://example.com/big.txt")]


def test_fetch_url_document_maps_remote_status_errors(monkeypatch):
    _install_async_client(
        monkeypatch,
        [
            _response(
                503,
                url="https://example.com/unavailable",
                headers={"content-type": "text/plain"},
                content=b"down",
            )
        ],
    )
    monkeypatch.setattr(
        "app.url_import.raise_for_private_or_meta_hosts",
        lambda _host: None,
    )

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(fetch_url_document("https://example.com/unavailable", _settings()))

    assert exc.value.status_code == 502
    assert exc.value.detail == "Remote server returned 503"


def test_fetch_url_document_preserves_markdown_extension(monkeypatch):
    _install_async_client(
        monkeypatch,
        [
            _response(
                200,
                url="https://example.com/readme",
                headers={"content-type": "text/markdown; charset=utf-8"},
                content=b"# Title\n\nBody",
            )
        ],
    )
    monkeypatch.setattr(
        "app.url_import.raise_for_private_or_meta_hosts",
        lambda _host: None,
    )

    body, ext = asyncio.run(fetch_url_document("https://example.com/readme", _settings()))

    assert body == b"# Title\n\nBody"
    assert ext == ".md"
