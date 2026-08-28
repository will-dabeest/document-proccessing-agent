import asyncio
import socket
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


def test_classify_image_and_xml_are_unsupported_binary():
    """Images and generic XML are not HTML/text/pdf; importing them must 415."""
    png = b"\x89PNG\r\n\x1a\n"
    xml = b"<?xml version='1.0'?><root/>"
    assert _classify_body("image/png", png) == "unknown_binary"
    assert _classify_body("image/jpeg", b"\xff\xd8\xff") == "unknown_binary"
    assert _classify_body("image/svg+xml", b"<svg xmlns='http://www.w3.org/2000/svg'/>") == (
        "unknown_binary"
    )
    assert _classify_body("application/xml", xml) == "unknown_binary"
    assert _classify_body("text/xml", xml) == "unknown_binary"


def test_classify_does_not_sniff_html_inside_plain_text():
    html = b"<html><body><p>should stay raw text</p></body></html>"
    assert _classify_body("text/plain", html) == "text"
    assert _classify_body("text/html", html) == "html"


def test_classify_pdf_magic_must_be_at_start_of_body():
    """Only a leading %PDF- marker is sniffed; indented/BOM-prefixed PDF is not."""
    assert _classify_body(None, b"%PDF-1.4\n") == "pdf"
    assert _classify_body(None, b"  %PDF-1.4\n") == "unknown_binary"
    assert _classify_body("text/plain", b"\xef\xbb\xbf%PDF-1.4\n") == "text"
    assert _classify_body("application/octet-stream", b"\n%PDF-1.4\n") == "unknown_binary"


def test_parse_tab_in_host_joins_labels():
    """HTAB in the authority is stripped by urlparse, joining labels into another host.

    The raw URL (passed to httpx) still contains the tab, so SSRF host checks and
    the fetch target can disagree.
    """
    raw, host, port = _parse_and_validate_url("https://example.com\t.evil.com/")
    assert host == "example.com.evil.com"
    assert "\t" in raw
    assert port is None


def test_parse_crlf_in_path_does_not_change_host():
    raw, host, port = _parse_and_validate_url(
        "https://example.com/docs\r\nX-Injected: 1"
    )
    assert host == "example.com"
    assert port is None
    assert "127.0.0.1" not in host


def test_parse_crlf_host_header_injection_does_not_succeed():
    """CR/LF in the authority must not parse as a clean example.com fetch target."""
    with pytest.raises((UrlImportError, ValueError)):
        _parse_and_validate_url("https://example.com\r\nHost: 127.0.0.1")


def test_raise_for_private_null_byte_truncates_to_loopback():
    """NUL in the hostname must not bypass SSRF (glibc getaddrinfo truncates at NUL)."""
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts("127.0.0.1\x00.example.com")
    assert exc.value.status_code in (400, 403)


def test_parse_preserves_embedded_nul_in_hostname():
    raw, host, port = _parse_and_validate_url("https://127.0.0.1\x00.example.com/secret")
    assert "\x00" in host
    assert port is None
    assert raw.startswith("https://")


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


def _public_dns(host, *args, **kwargs):
    return [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0)),
    ]


def test_fetch_empty_body_without_content_type_is_unsupported():
    client = _QueuedAsyncClient([_QueuedStreamResponse(chunks=(b"",))])
    with patch("app.url_import.httpx.AsyncClient", return_value=client), patch(
        "app.url_import.socket.getaddrinfo", side_effect=_public_dns
    ):
        with pytest.raises(UrlImportError) as exc:
            asyncio.run(
                fetch_url_document("https://example.com/empty", _fetch_settings())
            )
    assert exc.value.status_code == 415
    assert client.requested == [("GET", "https://example.com/empty")]


def test_fetch_image_png_is_unsupported():
    client = _QueuedAsyncClient(
        [
            _QueuedStreamResponse(
                headers={"content-type": "image/png"},
                chunks=(b"\x89PNG\r\n\x1a\n",),
            )
        ]
    )
    with patch("app.url_import.httpx.AsyncClient", return_value=client), patch(
        "app.url_import.socket.getaddrinfo", side_effect=_public_dns
    ):
        with pytest.raises(UrlImportError) as exc:
            asyncio.run(
                fetch_url_document("https://example.com/logo.png", _fetch_settings())
            )
    assert exc.value.status_code == 415


def test_fetch_httpx_client_does_not_disable_tls_verify():
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
    assert captured.get("verify", True) is not False
