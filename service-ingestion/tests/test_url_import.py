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


def test_classify_html_bytes_as_octet_stream_is_unknown_binary():
    """Only PDF magic is sniffed; HTML without an HTML content-type is not stored as text."""
    html = b"<html><body><p>public page</p></body></html>"
    assert _classify_body("application/octet-stream", html) == "unknown_binary"
    assert _classify_body(None, html) == "unknown_binary"


class _QueuedStreamCM:
    def __init__(self, inner):
        self._inner = inner

    async def __aenter__(self):
        return self._inner

    async def __aexit__(self, *_exc):
        return False


class _QueuedStreamResponse:
    def __init__(self, status_code=200, headers=None, chunks=()):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks

    async def aread(self):
        return b""

    def raise_for_status(self):
        return None

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class _QueuedAsyncClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requested = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def stream(self, method, url):
        self.requested.append((method, url))
        return _QueuedStreamCM(self._responses.pop(0))


def _fetch_settings(**overrides):
    values = {
        "url_import_enabled": True,
        "url_import_timeout_seconds": 5.0,
        "url_import_max_redirects": 5,
        "url_import_max_bytes": 1024,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _public_dns(host, *_a, **_k):
    assert host not in ("127.0.0.1", "localhost")
    return [(2, 1, 6, "", ("93.184.216.34", 0))]


def test_fetch_accepts_body_exactly_at_max_bytes():
    """Limit is exclusive (`total > max`); a body of exactly max_bytes must still be stored."""
    client = _QueuedAsyncClient(
        [
            _QueuedStreamResponse(
                headers={"content-type": "text/plain"},
                chunks=(b"1234", b"5678"),
            )
        ]
    )
    with patch("app.url_import.httpx.AsyncClient", return_value=client), patch(
        "app.url_import.socket.getaddrinfo", side_effect=_public_dns
    ):
        body, ext = asyncio.run(
            fetch_url_document(
                "https://example.com/exact",
                _fetch_settings(url_import_max_bytes=8),
            )
        )

    assert body == b"12345678"
    assert ext == ".txt"
    assert client.requested == [("GET", "https://example.com/exact")]


def test_fetch_parent_relative_redirect_stays_on_same_host():
    """`../` Location is resolved with urljoin and must not jump hosts."""
    client = _QueuedAsyncClient(
        [
            _QueuedStreamResponse(
                status_code=302, headers={"location": "../secret.txt"}
            ),
            _QueuedStreamResponse(
                headers={"content-type": "text/plain"},
                chunks=(b"ok",),
            ),
        ]
    )
    with patch("app.url_import.httpx.AsyncClient", return_value=client), patch(
        "app.url_import.socket.getaddrinfo", side_effect=_public_dns
    ):
        body, ext = asyncio.run(
            fetch_url_document(
                "https://example.com/docs/page",
                _fetch_settings(),
            )
        )

    assert body == b"ok"
    assert ext == ".txt"
    assert client.requested == [
        ("GET", "https://example.com/docs/page"),
        ("GET", "https://example.com/secret.txt"),
    ]


def test_fetch_octet_stream_html_is_unsupported():
    client = _QueuedAsyncClient(
        [
            _QueuedStreamResponse(
                headers={"content-type": "application/octet-stream"},
                chunks=(b"<html><body>public page</body></html>",),
            )
        ]
    )
    with patch("app.url_import.httpx.AsyncClient", return_value=client), patch(
        "app.url_import.socket.getaddrinfo", side_effect=_public_dns
    ):
        with pytest.raises(UrlImportError) as exc:
            asyncio.run(
                fetch_url_document("https://example.com/page", _fetch_settings())
            )

    assert exc.value.status_code == 415
    assert client.requested == [("GET", "https://example.com/page")]
