import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.url_import import (
    UrlImportError,
    _body_for_storage,
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


def _url_import_settings(**overrides):
    values = {
        "url_import_enabled": True,
        "url_import_max_bytes": 1024,
        "url_import_max_redirects": 2,
        "url_import_timeout_seconds": 1.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _response(*, status_code=200, headers=None, chunks=()):
    response = MagicMock()
    response.status_code = status_code
    response.headers = headers or {}
    response.aread = AsyncMock(return_value=b"")
    response.raise_for_status = MagicMock()

    async def _iter_bytes():
        for chunk in chunks:
            yield chunk

    response.aiter_bytes = _iter_bytes
    return response


class _FakeAsyncClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requested_urls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    @asynccontextmanager
    async def stream(self, method, url):
        assert method == "GET"
        self.requested_urls.append(url)
        yield next(self.responses)


def _public_dns_result(*_args, **_kwargs):
    return [(2, 1, 6, "", ("93.184.216.34", 0))]


def test_parse_strips_surrounding_whitespace():
    raw, host, port = _parse_and_validate_url("  https://example.com/a  ")
    assert raw == "https://example.com/a"
    assert host == "example.com"
    assert port is None


def test_parse_ipv6_loopback_url_exposes_unbracketed_host():
    """urlparse strips brackets; the SSRF checker must still see ::1."""
    raw, host, port = _parse_and_validate_url("http://[::1]/internal")
    assert host == "::1"
    assert port is None
    assert "[::1]" in raw
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(host)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "hostname",
    [
        "0.0.0.0",
        "::",
        "[::]",
        "240.0.0.1",
        "255.255.255.255",
    ],
)
def test_raise_for_private_blocks_unspecified_and_reserved_literals(hostname):
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts(hostname)
    assert exc.value.status_code == 403
    assert "disallowed" in exc.value.detail.lower()


def test_raise_for_private_rejects_invalid_bracketed_host():
    with pytest.raises(UrlImportError) as exc:
        raise_for_private_or_meta_hosts("[not-an-ip]")
    assert exc.value.status_code == 400
    assert exc.value.detail == "Invalid host"


def test_classify_markdown_vendor_content_type():
    assert _classify_body("application/vnd.github.markdown", b"# Title") == "text"


def test_body_for_storage_markdown_substring_uses_md_extension():
    body, ext = _body_for_storage(
        "text",
        b"# Title\n",
        "application/vnd.github.markdown",
    )
    assert body == b"# Title\n"
    assert ext == ".md"


@pytest.mark.parametrize(
    "location",
    [
        "//127.0.0.1/secret",
        "//169.254.169.254/latest/meta-data",
    ],
)
def test_fetch_rejects_protocol_relative_redirect_to_meta_hosts(location):
    """Protocol-relative Location (//host) keeps https but must revalidate the new host."""
    client = _FakeAsyncClient(
        [
            _response(status_code=302, headers={"location": location}),
            _response(headers={"content-type": "text/plain"}, chunks=(b"should-not-read",)),
        ]
    )
    with patch("app.url_import.httpx.AsyncClient", return_value=client), patch(
        "app.url_import.socket.getaddrinfo", side_effect=_public_dns_result
    ):
        with pytest.raises(UrlImportError) as exc:
            asyncio.run(
                fetch_url_document(
                    "https://example.com/start",
                    _url_import_settings(),
                )
            )

    assert exc.value.status_code == 403
    assert client.requested_urls == ["https://example.com/start"]


def test_fetch_rejects_redirect_to_file_scheme():
    client = _FakeAsyncClient(
        [
            _response(
                status_code=302,
                headers={"location": "file:///etc/passwd"},
            ),
        ]
    )
    with patch("app.url_import.httpx.AsyncClient", return_value=client), patch(
        "app.url_import.socket.getaddrinfo", side_effect=_public_dns_result
    ):
        with pytest.raises(UrlImportError) as exc:
            asyncio.run(
                fetch_url_document(
                    "https://example.com/start",
                    _url_import_settings(),
                )
            )

    assert exc.value.status_code == 400
    assert "http" in exc.value.detail.lower()
    assert client.requested_urls == ["https://example.com/start"]
