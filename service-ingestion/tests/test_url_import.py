import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from app.url_import import (
    MAX_URL_LENGTH,
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


def test_parse_url_length_limit_is_exclusive():
    """Reject only when len(stripped) > MAX_URL_LENGTH (2048), not at equality."""
    prefix = "https://example.com/"
    at_limit = prefix + ("a" * (MAX_URL_LENGTH - len(prefix)))
    assert len(at_limit) == MAX_URL_LENGTH

    raw, host, port = _parse_and_validate_url(at_limit)
    assert raw == at_limit
    assert host == "example.com"
    assert port is None

    with pytest.raises(UrlImportError) as exc:
        _parse_and_validate_url(at_limit + "x")
    assert exc.value.status_code == 400
    assert exc.value.detail == "URL is too long"


def test_parse_url_length_limit_uses_stripped_value():
    prefix = "https://example.com/"
    at_limit = prefix + ("a" * (MAX_URL_LENGTH - len(prefix)))
    raw, host, _port = _parse_and_validate_url(" \t" + at_limit + " \n")
    assert raw == at_limit
    assert host == "example.com"


def test_classify_pdf_content_type_ignores_charset_parameter():
    # No %PDF- magic: classification must come from the type after stripping parameters.
    assert _classify_body("application/pdf; charset=binary", b"%not-pdf-magic") == "pdf"


def test_fetch_disabled_does_not_resolve_or_request():
    """Disabled import must 403 before DNS or HTTP — otherwise SSRF controls are skipped."""
    with patch("app.url_import.httpx.AsyncClient") as client_cls, patch(
        "app.url_import.socket.getaddrinfo"
    ) as dns:
        with pytest.raises(UrlImportError) as exc:
            asyncio.run(
                fetch_url_document(
                    "https://127.0.0.1/secret",
                    SimpleNamespace(url_import_enabled=False),
                )
            )

    assert exc.value.status_code == 403
    assert exc.value.detail == "URL import is disabled"
    dns.assert_not_called()
    client_cls.assert_not_called()


def test_fetch_httpx_client_caps_connection_pool():
    captured = {}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        def stream(self, method, url):
            raise httpx.RequestError("stop", request=httpx.Request(method, url))

    def _factory(*_a, **kwargs):
        captured.update(kwargs)
        return _Client()

    def _public_dns(host, *_a, **_k):
        assert host == "example.com"
        return [(2, 1, 6, "", ("93.184.216.34", 0))]

    settings = SimpleNamespace(
        url_import_enabled=True,
        url_import_timeout_seconds=7.5,
        url_import_max_redirects=5,
        url_import_max_bytes=1024,
    )
    with patch("app.url_import.httpx.AsyncClient", side_effect=_factory), patch(
        "app.url_import.socket.getaddrinfo", side_effect=_public_dns
    ):
        with pytest.raises(UrlImportError) as exc:
            asyncio.run(fetch_url_document("https://example.com/doc", settings))

    assert exc.value.status_code == 502
    limits = captured["limits"]
    assert limits.max_connections == 5
    assert limits.max_keepalive_connections == 5
