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


@pytest.mark.parametrize(
    "url",
    [
        "mailto:user@example.com",
        "ws://example.com/socket",
        "wss://example.com/socket",
        "gopher://example.com/1",
    ],
)
def test_parse_rejects_non_http_schemes(url):
    with pytest.raises(UrlImportError) as exc:
        _parse_and_validate_url(url)
    assert exc.value.status_code == 400
    assert "http" in exc.value.detail.lower()


def test_parse_fragment_does_not_override_host():
    raw, host, port = _parse_and_validate_url("https://example.com/docs#@127.0.0.1")
    assert host == "example.com"
    assert port is None
    assert "127.0.0.1" not in host
    assert "#@127.0.0.1" in raw


def test_raise_for_private_blocks_loopback():
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts("127.0.0.1")
    assert exc.value.status_code == 403


def test_raise_for_private_blocks_hostname_localhost():
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts("localhost")
    assert exc.value.status_code == 403


@pytest.mark.parametrize("hostname", ["127.0.0.2", "127.255.255.255"])
def test_raise_for_private_blocks_loopback_range_not_just_dot_one(hostname):
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(hostname)
    assert exc.value.status_code == 403
    assert "disallowed" in exc.value.detail.lower()


def test_classify_pdf_magic_overrides_octet_stream():
    assert _classify_body("application/octet-stream", b"%PDF-1.4\n1 0 obj") == "pdf"


def test_classify_html():
    assert _classify_body("text/html; charset=utf-8", b"<html></html>") == "html"


def test_classify_uppercase_content_types():
    assert _classify_body("TEXT/HTML; CHARSET=UTF-8", b"<html></html>") == "html"
    assert _classify_body("TEXT/PLAIN; CHARSET=UTF-8", b"hello") == "text"


def test_classify_unknown_binary():
    assert _classify_body("application/zip", b"PK\x03\x04") == "unknown_binary"


def test_html_to_plain_text_trafilatura_smoke():
    from app.url_import import _html_to_plain_text

    raw = b"<html><body><article><p>UniqueMarkerAlpha beta</p></article></body></html>"
    out = _html_to_plain_text(raw)
    assert b"UniqueMarkerAlpha" in out


def test_url_import_security_defaults(monkeypatch):
    for key in (
        "URL_IMPORT_ENABLED",
        "URL_IMPORT_MAX_BYTES",
        "URL_IMPORT_MAX_REDIRECTS",
        "URL_IMPORT_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)
    from app.config import Settings

    s = Settings(_env_file=None)
    assert s.url_import_enabled is True
    assert s.url_import_max_bytes == 10 * 1024 * 1024
    assert s.url_import_max_redirects == 5
    assert s.url_import_timeout_seconds == 30.0


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


def test_fetch_follows_https_to_http_redirect_and_revalidates_host():
    client = _FakeAsyncClient(
        [
            _StreamResponse(
                status_code=302,
                headers={"location": "http://cdn.example.net/doc.txt"},
            ),
            _StreamResponse(
                headers={"content-type": "text/plain"},
                chunks=(b"plain body",),
            ),
        ]
    )
    settings = SimpleNamespace(
        url_import_enabled=True,
        url_import_timeout_seconds=5.0,
        url_import_max_redirects=5,
        url_import_max_bytes=1024,
    )

    def _public_dns(host, *_a, **_k):
        assert host in ("example.com", "cdn.example.net")
        return [(2, 1, 6, "", ("93.184.216.34", 0))]

    with patch("app.url_import.httpx.AsyncClient", return_value=client), patch(
        "app.url_import.socket.getaddrinfo", side_effect=_public_dns
    ):
        body, ext = asyncio.run(
            fetch_url_document("https://example.com/a", settings)
        )

    assert body == b"plain body"
    assert ext == ".txt"
    assert client.requested == [
        ("GET", "https://example.com/a"),
        ("GET", "http://cdn.example.net/doc.txt"),
    ]
