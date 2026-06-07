import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.url_import import (
    UrlImportError,
    _classify_body,
    _parse_and_validate_url,
    fetch_url_document,
    raise_for_private_or_meta_hosts,
)


class _FakeStreamContext:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeResponse:
    def __init__(self, status_code, headers=None, chunks=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks or []

    async def aread(self):
        return b"".join(self._chunks)

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    def raise_for_status(self):
        return None


class _FakeAsyncClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requested_urls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, method, url):
        self.requested_urls.append((method, url))
        return _FakeStreamContext(self._responses.pop(0))


def _url_import_settings(**overrides):
    values = {
        "url_import_enabled": True,
        "url_import_max_bytes": 1024,
        "url_import_max_redirects": 5,
        "url_import_timeout_seconds": 5.0,
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


def test_fetch_url_document_revalidates_redirect_target_before_request():
    client = _FakeAsyncClient(
        [
            _FakeResponse(302, headers={"location": "http://127.0.0.1/internal"}),
            _FakeResponse(200, headers={"content-type": "text/plain"}, chunks=[b"secret"]),
        ]
    )
    checked_hosts = []

    def validate_host(host):
        checked_hosts.append(host)
        if host == "127.0.0.1":
            raise UrlImportError(403, "URL resolves to a disallowed address")

    async def run():
        with patch("app.url_import.httpx.AsyncClient", return_value=client), patch(
            "app.url_import.raise_for_private_or_meta_hosts", side_effect=validate_host
        ):
            with pytest.raises(UrlImportError) as exc:
                await fetch_url_document("https://example.com/start", _url_import_settings())
        assert exc.value.status_code == 403

    asyncio.run(run())
    assert checked_hosts == ["example.com", "127.0.0.1"]
    assert client.requested_urls == [("GET", "https://example.com/start")]


def test_fetch_url_document_rejects_oversized_stream():
    client = _FakeAsyncClient(
        [
            _FakeResponse(
                200,
                headers={"content-type": "text/plain"},
                chunks=[b"abc", b"def"],
            )
        ]
    )

    async def run():
        with patch("app.url_import.httpx.AsyncClient", return_value=client), patch(
            "app.url_import.raise_for_private_or_meta_hosts"
        ):
            with pytest.raises(UrlImportError) as exc:
                await fetch_url_document(
                    "https://example.com/large",
                    _url_import_settings(url_import_max_bytes=5),
                )
        assert exc.value.status_code == 413

    asyncio.run(run())
    assert client.requested_urls == [("GET", "https://example.com/large")]


def test_fetch_url_document_stops_after_redirect_limit():
    client = _FakeAsyncClient(
        [
            _FakeResponse(302, headers={"location": "/one"}),
            _FakeResponse(302, headers={"location": "/two"}),
        ]
    )

    async def run():
        with patch("app.url_import.httpx.AsyncClient", return_value=client), patch(
            "app.url_import.raise_for_private_or_meta_hosts"
        ):
            with pytest.raises(UrlImportError) as exc:
                await fetch_url_document(
                    "https://example.com/start",
                    _url_import_settings(url_import_max_redirects=1),
                )
        assert exc.value.status_code == 502
        assert exc.value.detail == "Too many redirects"

    asyncio.run(run())
    assert client.requested_urls == [
        ("GET", "https://example.com/start"),
        ("GET", "https://example.com/one"),
    ]
