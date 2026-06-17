import asyncio
import socket

import httpx
import pytest

from app.config import settings
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


@pytest.mark.parametrize(
    "hostname",
    [
        "10.0.0.1",
        "169.254.169.254",
        "[::1]",
    ],
)
def test_raise_for_private_blocks_disallowed_literal_addresses(hostname):
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(hostname)
    assert exc.value.status_code == 403


def test_raise_for_private_blocks_hostname_localhost():
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts("localhost")
    assert exc.value.status_code == 403


def test_raise_for_private_blocks_dns_resolved_private_address(monkeypatch):
    def fake_getaddrinfo(host, *_args, **_kwargs):
        assert host == "safe-name.example"
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("10.0.0.5", 443),
            )
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts("safe-name.example")

    assert exc.value.status_code == 403


def test_fetch_url_document_revalidates_redirect_target_before_request(monkeypatch):
    requested_urls = []

    def handler(request):
        requested_urls.append(str(request.url))
        return httpx.Response(
            302,
            headers={"Location": "http://127.0.0.1/private"},
            request=request,
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def async_client_with_mock_transport(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    test_settings = settings.model_copy(update={"url_import_max_redirects": 1})
    monkeypatch.setattr(httpx, "AsyncClient", async_client_with_mock_transport)

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(fetch_url_document("https://93.184.216.34/start", test_settings))

    assert exc.value.status_code == 403
    assert requested_urls == ["https://93.184.216.34/start"]


def test_fetch_url_document_enforces_streamed_body_limit(monkeypatch):
    def handler(request):
        return httpx.Response(
            200,
            content=b"abcdef",
            headers={"Content-Type": "text/plain; charset=utf-8"},
            request=request,
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def async_client_with_mock_transport(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    test_settings = settings.model_copy(update={"url_import_max_bytes": 5})
    monkeypatch.setattr(httpx, "AsyncClient", async_client_with_mock_transport)

    with pytest.raises(UrlImportError) as exc:
        asyncio.run(fetch_url_document("https://93.184.216.34/big", test_settings))

    assert exc.value.status_code == 413


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
