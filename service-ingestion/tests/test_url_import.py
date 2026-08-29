import asyncio
import socket
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from app.url_import import (
    UrlImportError,
    _body_for_storage,
    _classify_body,
    _html_to_plain_text,
    _normalize_text_body,
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
    raw = b"<html><body><article><p>UniqueMarkerAlpha beta</p></article></body></html>"
    out = _html_to_plain_text(raw)
    assert b"UniqueMarkerAlpha" in out


def test_html_to_plain_text_replaces_invalid_utf8():
    """Mis-encoded HTML must still extract as valid UTF-8, not raise into a 500."""
    raw = (
        b"<html><body><article><p>UniqueMarkerZed caf\xe9</p></article></body></html>"
    )
    out = _html_to_plain_text(raw)
    text = out.decode("utf-8")
    assert "UniqueMarkerZed" in text
    assert b"\xe9" not in out
    body, ext = _body_for_storage("html", raw, "text/html")
    assert ext == ".txt"
    assert body.decode("utf-8")


def test_normalize_text_body_replaces_invalid_utf8():
    """Plain-text/markdown imports re-encode replacement chars so /import-url can index."""
    raw = b"hello \xff world"
    out = _normalize_text_body(raw)
    text = out.decode("utf-8")
    assert "\ufffd" in text
    assert b"\xff" not in out

    stored, ext = _body_for_storage("text", raw, "text/plain")
    assert ext == ".txt"
    assert stored.decode("utf-8")

    md_body, md_ext = _body_for_storage("text", b"# Title \xff", "text/markdown")
    assert md_ext == ".md"
    assert md_body.decode("utf-8")


@pytest.mark.parametrize(
    "url",
    [
        "smb://fileserver/share/secret.txt",
        "ssh://example.com/etc/passwd",
        "telnet://example.com:23/",
        "ldap://ldap.example.com/dc=ex",
        "blob:https://example.com/11111111-1111-1111-1111-111111111111",
        "about:blank",
        "view-source:https://example.com/secret",
        "http+unix://%2Fvar%2Frun%2Fdocker.sock/secret",
    ],
)
def test_parse_rejects_non_web_schemes(url):
    """Only http/https are fetched; unix/SMB/view-source schemes must not reach DNS/HTTP."""
    with pytest.raises(UrlImportError) as exc:
        _parse_and_validate_url(url)
    assert exc.value.status_code == 400
    assert "http" in exc.value.detail.lower()


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


class _RaisingAsyncClient:
    def __init__(self, exc):
        self.exc = exc
        self.requested = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def stream(self, method, url):
        self.requested.append((method, url))
        raise self.exc


def _fetch_settings(**overrides):
    values = {
        "url_import_enabled": True,
        "url_import_timeout_seconds": 5.0,
        "url_import_max_redirects": 5,
        "url_import_max_bytes": 1024,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _public_dns(host, *args, **kwargs):
    return [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0)),
    ]


def test_fetch_invalid_utf8_plain_text_is_stored_as_utf8():
    client = _QueuedAsyncClient(
        [
            _QueuedStreamResponse(
                headers={"content-type": "text/plain"},
                chunks=(b"alpha \xff omega UniqueMarkerText",),
            )
        ]
    )
    with patch("app.url_import.httpx.AsyncClient", return_value=client), patch(
        "app.url_import.socket.getaddrinfo", side_effect=_public_dns
    ):
        body, ext = asyncio.run(
            fetch_url_document("https://example.com/notes.txt", _fetch_settings())
        )

    assert ext == ".txt"
    text = body.decode("utf-8")
    assert "UniqueMarkerText" in text
    assert "\ufffd" in text
    assert client.requested == [("GET", "https://example.com/notes.txt")]


def test_fetch_read_timeout_is_bad_gateway():
    request = httpx.Request("GET", "https://example.com/slow")
    client = _RaisingAsyncClient(httpx.ReadTimeout("timed out", request=request))
    with patch("app.url_import.httpx.AsyncClient", return_value=client), patch(
        "app.url_import.socket.getaddrinfo", side_effect=_public_dns
    ):
        with pytest.raises(UrlImportError) as exc:
            asyncio.run(
                fetch_url_document("https://example.com/slow", _fetch_settings())
            )
    assert exc.value.status_code == 502
    assert exc.value.detail.startswith("Could not fetch URL:")
    assert client.requested == [("GET", "https://example.com/slow")]


def test_fetch_httpx_client_uses_configured_timeout():
    captured = {}
    inner = _QueuedAsyncClient(
        [_QueuedStreamResponse(headers={"content-type": "text/plain"}, chunks=(b"ok",))],
    )

    def _factory(*_args, **kwargs):
        captured.update(kwargs)
        return inner

    with patch("app.url_import.httpx.AsyncClient", side_effect=_factory), patch(
        "app.url_import.socket.getaddrinfo", side_effect=_public_dns
    ):
        body, ext = asyncio.run(
            fetch_url_document("https://example.com/ok.txt", _fetch_settings())
        )

    assert body == b"ok"
    assert ext == ".txt"
    timeout = captured["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout == httpx.Timeout(5.0)
    assert timeout.connect == timeout.read == 5.0
    assert captured["follow_redirects"] is False
