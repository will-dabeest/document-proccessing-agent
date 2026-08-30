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


def test_parse_preserves_trailing_dot_on_loopback_host():
    """FQDN form of loopback is not normalized by urlparse; SSRF must still see it."""
    raw, host, port = _parse_and_validate_url("https://127.0.0.1./secret")
    assert host == "127.0.0.1."
    assert port is None
    assert "127.0.0.1." in raw


@pytest.mark.parametrize(
    "hostname, resolved",
    [
        ("127.0.0.1.", "127.0.0.1"),
        ("localhost.", "127.0.0.1"),
        ("0.0.0.0.", "0.0.0.0"),
    ],
)
def test_raise_for_private_blocks_trailing_dot_hosts_via_dns(
    hostname, resolved, monkeypatch
):
    """Trailing dots skip ipaddress literals; DNS still returns a disallowed address."""
    seen: list[str] = []

    def fake_getaddrinfo(host, *args, **kwargs):
        seen.append(host)
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (resolved, 0)),
        ]

    monkeypatch.setattr("app.url_import.socket.getaddrinfo", fake_getaddrinfo)
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(hostname)
    assert exc.value.status_code == 403
    assert seen == [hostname]


def test_raise_for_private_allows_trailing_dot_public_host(monkeypatch):
    """A trailing-dot public FQDN must resolve, not be blanket-rejected as invalid."""

    def fake_getaddrinfo(host, *args, **kwargs):
        assert host == "example.com."
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0)),
        ]

    monkeypatch.setattr("app.url_import.socket.getaddrinfo", fake_getaddrinfo)
    raise_for_private_or_meta_hosts("example.com.")


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


def test_fetch_trailing_dot_loopback_does_not_issue_http():
    """SSRF must reject https://127.0.0.1./ before any GET (ipaddress skips trailing dots)."""
    client = _RaisingAsyncClient(AssertionError("GET must not run"))
    with patch("app.url_import.httpx.AsyncClient", return_value=client), patch(
        "app.url_import.socket.getaddrinfo",
        return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0)),
        ],
    ):
        with pytest.raises(UrlImportError) as exc:
            asyncio.run(
                fetch_url_document(
                    "https://127.0.0.1./secret", _fetch_settings()
                )
            )
    assert exc.value.status_code == 403
    assert client.requested == []


@pytest.mark.parametrize(
    "exc_factory",
    [
        lambda req: httpx.ConnectTimeout("connect timed out", request=req),
        lambda req: httpx.WriteTimeout("write timed out", request=req),
        lambda req: httpx.PoolTimeout("pool timed out", request=req),
        lambda req: httpx.RemoteProtocolError("server disconnected", request=req),
        lambda req: httpx.ProxyError("proxy failed", request=req),
        lambda req: httpx.UnsupportedProtocol("unsupported protocol", request=req),
    ],
    ids=[
        "connect_timeout",
        "write_timeout",
        "pool_timeout",
        "remote_protocol",
        "proxy",
        "unsupported_protocol",
    ],
)
def test_fetch_transport_errors_are_bad_gateway(exc_factory):
    """Non-ReadTimeout RequestErrors must map to 502, not leak as unhandled 500s."""
    request = httpx.Request("GET", "https://example.com/doc")
    client = _RaisingAsyncClient(exc_factory(request))
    with patch("app.url_import.httpx.AsyncClient", return_value=client), patch(
        "app.url_import.socket.getaddrinfo", side_effect=_public_dns
    ):
        with pytest.raises(UrlImportError) as exc:
            asyncio.run(
                fetch_url_document("https://example.com/doc", _fetch_settings())
            )
    assert exc.value.status_code == 502
    assert exc.value.detail.startswith("Could not fetch URL:")
    assert client.requested == [("GET", "https://example.com/doc")]
