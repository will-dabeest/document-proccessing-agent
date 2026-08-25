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


@pytest.mark.parametrize("hostname", ["", "   "])
def test_raise_for_private_rejects_empty_or_whitespace_host(hostname):
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(hostname)
    assert exc.value.status_code == 400
    assert "host" in exc.value.detail.lower()


class _AsyncCM:
    def __init__(self, inner):
        self._inner = inner

    async def __aenter__(self):
        return self._inner

    async def __aexit__(self, *_exc):
        return False


class _StreamResponse:
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


class _FakeAsyncClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requested = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def stream(self, method, url):
        self.requested.append((method, url))
        return _AsyncCM(self._responses.pop(0))


def _public_settings():
    return SimpleNamespace(
        url_import_enabled=True,
        url_import_timeout_seconds=5.0,
        url_import_max_redirects=5,
        url_import_max_bytes=1024,
    )


def _public_dns(host, *_a, **_k):
    assert host not in ("127.0.0.1", "localhost")
    return [(2, 1, 6, "", ("93.184.216.34", 0))]


def test_fetch_rejects_empty_location_header_without_follow_up_get():
    client = _FakeAsyncClient(
        [
            _StreamResponse(status_code=302, headers={"location": ""}),
            _StreamResponse(
                headers={"content-type": "text/plain"},
                chunks=(b"should-not-read",),
            ),
        ]
    )
    with patch("app.url_import.httpx.AsyncClient", return_value=client), patch(
        "app.url_import.socket.getaddrinfo", side_effect=_public_dns
    ):
        with pytest.raises(UrlImportError) as exc:
            asyncio.run(
                fetch_url_document("https://example.com/start", _public_settings())
            )

    assert exc.value.status_code == 502
    assert "location" in exc.value.detail.lower()
    assert client.requested == [("GET", "https://example.com/start")]


def test_fetch_does_not_follow_whitespace_prefixed_redirect_to_loopback():
    """Leading spaces in Location keep urljoin from treating it as an http URL."""
    client = _FakeAsyncClient(
        [
            _StreamResponse(
                status_code=302,
                headers={"location": "  https://127.0.0.1/secret"},
            ),
        ]
    )
    with patch("app.url_import.httpx.AsyncClient", return_value=client), patch(
        "app.url_import.socket.getaddrinfo", side_effect=_public_dns
    ):
        with pytest.raises(UrlImportError) as exc:
            asyncio.run(
                fetch_url_document("https://example.com/start", _public_settings())
            )

    assert exc.value.status_code in (400, 403)
    assert client.requested == [("GET", "https://example.com/start")]
    assert not any("127.0.0.1" in url for _method, url in client.requested)


def test_fetch_query_only_redirect_preserves_path():
    client = _FakeAsyncClient(
        [
            _StreamResponse(status_code=302, headers={"location": "?download=1"}),
            _StreamResponse(
                headers={"content-type": "text/plain"},
                chunks=(b"ok",),
            ),
        ]
    )
    with patch("app.url_import.httpx.AsyncClient", return_value=client), patch(
        "app.url_import.socket.getaddrinfo", side_effect=_public_dns
    ):
        body, ext = asyncio.run(
            fetch_url_document("https://example.com/docs/page", _public_settings())
        )

    assert body == b"ok"
    assert ext == ".txt"
    assert client.requested == [
        ("GET", "https://example.com/docs/page"),
        ("GET", "https://example.com/docs/page?download=1"),
    ]


def test_fetch_storage_extension_ignores_url_path_and_content_disposition():
    client = _FakeAsyncClient(
        [
            _StreamResponse(
                headers={
                    "content-type": "text/plain; charset=utf-8",
                    "content-disposition": 'attachment; filename="malware.exe"',
                },
                chunks=(b" innocuous text ",),
            ),
        ]
    )
    with patch("app.url_import.httpx.AsyncClient", return_value=client), patch(
        "app.url_import.socket.getaddrinfo", side_effect=_public_dns
    ):
        body, ext = asyncio.run(
            fetch_url_document(
                "https://example.com/downloads/payload.exe", _public_settings()
            )
        )

    assert ext == ".txt"
    assert body == b" innocuous text "
    assert client.requested == [("GET", "https://example.com/downloads/payload.exe")]
