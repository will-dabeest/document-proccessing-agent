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


REAL_ASYNC_CLIENT = httpx.AsyncClient


def _settings(**overrides):
    values = {
        "url_import_enabled": True,
        "url_import_max_bytes": 1024,
        "url_import_max_redirects": 5,
        "url_import_timeout_seconds": 5.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _client_factory(transport):
    def factory(**kwargs):
        return REAL_ASYNC_CLIENT(
            transport=transport,
            follow_redirects=kwargs["follow_redirects"],
            headers=kwargs["headers"],
            timeout=kwargs["timeout"],
        )

    return factory


def _public_dns(*_args, **_kwargs):
    return [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            6,
            "",
            ("93.184.216.34", 443),
        )
    ]


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


def test_parse_rejects_invalid_port_with_url_import_error():
    with pytest.raises(UrlImportError) as exc:
        _parse_and_validate_url("https://example.com:99999/doc")

    assert exc.value.status_code == 400
    assert exc.value.detail == "Invalid port"


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


def test_fetch_revalidates_redirect_target_and_blocks_private_ip():
    seen_urls = []

    async def handler(request):
        seen_urls.append(str(request.url))
        return httpx.Response(
            302,
            headers={"Location": "http://169.254.169.254/latest/meta-data"},
        )

    transport = httpx.MockTransport(handler)
    with patch("app.url_import.httpx.AsyncClient", new=_client_factory(transport)), patch(
        "app.url_import.socket.getaddrinfo",
        side_effect=_public_dns,
    ):
        with pytest.raises(UrlImportError) as exc:
            asyncio.run(fetch_url_document("https://example.com/start", _settings()))

    assert exc.value.status_code == 403
    assert seen_urls == ["https://example.com/start"]


def test_fetch_rejects_response_larger_than_configured_limit():
    async def handler(_request):
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            content=b"abcdef",
        )

    transport = httpx.MockTransport(handler)
    with patch("app.url_import.httpx.AsyncClient", new=_client_factory(transport)), patch(
        "app.url_import.socket.getaddrinfo",
        side_effect=_public_dns,
    ):
        with pytest.raises(UrlImportError) as exc:
            asyncio.run(
                fetch_url_document(
                    "https://example.com/large.txt",
                    _settings(url_import_max_bytes=3),
                )
            )

    assert exc.value.status_code == 413


def test_fetch_preserves_markdown_extension_and_normalizes_text():
    async def handler(_request):
        return httpx.Response(
            200,
            headers={"content-type": "text/markdown; charset=utf-8"},
            content="# Title\nBody",
        )

    transport = httpx.MockTransport(handler)
    with patch("app.url_import.httpx.AsyncClient", new=_client_factory(transport)), patch(
        "app.url_import.socket.getaddrinfo",
        side_effect=_public_dns,
    ):
        body, ext = asyncio.run(
            fetch_url_document("https://example.com/readme.md", _settings())
        )

    assert body == b"# Title\nBody"
    assert ext == ".md"
